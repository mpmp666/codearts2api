# -*- coding: utf-8 -*-
"""DPoP（RFC 9449）证明 JWT：ES256 + P-256。

为什么单独一个模块：华为云 STS 把 refresh_token 绑定到**首次换取凭证时的 DPoP 公钥**，
换密钥去刷新会被直接拒绝（`invalid refresh token: InvalidDPoPHeader`）。所以私钥必须
与 refresh_token 一起持久化并在每次刷新时原样复用，这就要求私钥的序列化/反序列化
（JWK）足够严谨 —— 独立成模块便于单测覆盖「公钥私钥必须匹配」这类不变量。

对齐 Go 版 `internal/upstream/dpop.go`：
  - JWK 字段 kty/crv/x/y/d，x/y/d 都是 32 字节定长、base64url 无填充
  - 私钥恢复后必须校验 d*G == (x, y)，否则说明文件被改过
  - DPoP JWT header: {alg:ES256, typ:dpop+jwt, jwk:{公钥}}
  - payload: {htm:POST, htu:<token 端点>, iat, jti}
  - 签名做低 S 归一化（jose 默认低 S，服务端兼容性更好）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# P-256 曲线阶（低 S 归一化用）
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

# 32 字节定长编码，对应 Go 的 padded()
_FIELD_BYTES = 32


class DPoPError(RuntimeError):
    """DPoP 密钥或证明生成失败。"""


def _b64u(raw: bytes) -> str:
    """base64url 无填充（JOSE 要求）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    """base64url 解码，自动补齐被剥掉的 '=' 填充。"""
    if not isinstance(value, str):
        raise DPoPError("JWK 字段必须是字符串")
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except (ValueError, TypeError) as error:  # binascii.Error 属于 ValueError 子类
        raise DPoPError(f"JWK 字段不是合法 base64url: {error}") from error


def _padded(value: int) -> bytes:
    """把整数补成 32 字节大端（Go 的 padded() 等价实现）。"""
    return value.to_bytes(_FIELD_BYTES, "big")


def _random_hex(nbytes: int) -> str:
    """生成 nbytes 字节的随机 hex 串（jti 用）。"""
    return os.urandom(nbytes).hex()


def new_private_jwk() -> dict:
    """生成可持久化的 DPoP 私钥 JWK（含 d）。

    返回 dict 而不是对象：它要原样写进 auths/codearts-{uid}.json，与 Go 版字段对齐。
    """
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.private_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u(_padded(numbers.public_numbers.x)),
        "y": _b64u(_padded(numbers.public_numbers.y)),
        "d": _b64u(_padded(numbers.private_value)),
    }


def _key_from_jwk(jwk: dict) -> ec.EllipticCurvePrivateKey:
    """从 JWK 恢复私钥，并校验公私钥一致性。"""
    if not isinstance(jwk, dict) or not jwk.get("d"):
        raise DPoPError("DPoP 私钥缺失（d 字段为空），需要重新登录")
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise DPoPError("DPoP JWK 曲线不受支持，只接受 EC/P-256")

    def field(name: str) -> bytes:
        raw = _b64u_decode(jwk.get(name, ""))
        if len(raw) != _FIELD_BYTES:
            raise DPoPError(f"DPoP JWK 字段 {name} 长度不是 {_FIELD_BYTES} 字节")
        return raw

    d_raw = field("d")
    x_raw = field("x")
    y_raw = field("y")
    d = int.from_bytes(d_raw, "big")
    if d <= 0 or d >= _P256_N:
        raise DPoPError("DPoP JWK 私钥标量越界")
    key = ec.derive_private_key(d, ec.SECP256R1())
    public = key.public_key().public_numbers()
    # 公钥/私钥必须成对：文件被手工改过或写坏了都要在刷新前就发现，
    # 否则会拿一个必然被 STS 拒绝的密钥去发请求。
    if public.x != int.from_bytes(x_raw, "big") or public.y != int.from_bytes(y_raw, "big"):
        raise DPoPError("DPoP JWK 公钥与私钥不匹配")
    return key


def public_jwk(jwk: dict) -> dict:
    """取 JWK 的公钥部分（DPoP proof header 里要带）。"""
    return {k: jwk[k] for k in ("kty", "crv", "x", "y") if k in jwk}


def has_private_key(jwk) -> bool:
    """判断一份 JWK 是否可用（有 d 字段且曲线正确）。"""
    return isinstance(jwk, dict) and bool(jwk.get("d")) and jwk.get("kty") == "EC"


def sign_proof(jwk: dict, htu: str, htm: str = "POST") -> str:
    """生成 DPoP 证明 JWT（紧凑序列化）。

    htu 必须是**不带 query** 的完整 token 端点地址：华为 STS 按原样比对。
    """
    key = _key_from_jwk(jwk)
    header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_jwk(jwk)}
    payload = {
        "htm": htm,
        "htu": htu,
        "iat": int(time.time()),
        "jti": _random_hex(32),
    }
    # separators 必须紧凑且 ensure_ascii=False：签名覆盖的是这段字节，多一个空格都会变
    header_json = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signing_input = _b64u(header_json) + "." + _b64u(payload_json)
    digest = hashlib.sha256(signing_input.encode("ascii")).digest()
    # ⚠️ 这里传进去的**已经是摘要**，必须用 Prehashed。
    # `ec.ECDSA(hashes.SHA256())` 的语义是「先对传入数据做 SHA256 再签名」，
    # 直接用它会把 digest 再哈希一遍，签名实际覆盖 SHA256(SHA256(input))，
    # 而 JOSE/RFC 9449 要求覆盖 SHA256(input) —— 服务端（华为 STS）会拒绝每一个
    # DPoP 证明，报错却伪装成 InvalidDPoPHeader / 密钥不对，极难排查。
    # 这个 bug 只做「自己签自己验」的 round-trip 是抓不到的（两边都用错语义照样通过），
    # 必须像服务端那样用 Prehashed 独立验签 —— 见 tests 里的独立验签用例。
    der = key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    r, s = decode_dss_signature(der)
    if s > _P256_N // 2:
        # 低 S 归一化：部分服务端（jose 系）只接受低 S 签名
        s = _P256_N - s
    signature = _padded(r) + _padded(s)
    return signing_input + "." + _b64u(signature)
