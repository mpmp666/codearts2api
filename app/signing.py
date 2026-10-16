# -*- coding: utf-8 -*-
"""华为云 SDK-HMAC-SHA256 请求签名（永久 AK/SK 与 STS 临时凭证通用）。

为什么要两种入口：
  * `sign_request()` —— 通用签名器，对齐 Go 版 `internal/upstream/signer.go`。用于
    agent-center 模型发现、积分活动、opengw 福利网关、caller-identity 等接口。
    它把「本次请求实际带的头」全部参与签名（小写排序），并在有 security_token 时
    自动补 `x-security-token`（临时凭证必须随请求下发，且必须计入 SignedHeaders）。
  * `chat_headers()` —— 聊天专用。**逐字节保留基线行为**：基线抓包确认
    `/api/v2/chat/completions` 只需签 host/content-type/x-sdk-date，benefit 通道再额外
    签 `maas_type`/`model-id`/`model-name`。多签一个头就可能被上游判签名不符，所以
    这条路径的头部集合是冻结的；OAuth 凭证下再补两个上游要求的新头。

签名规范（两种入口共用 `_build_signature`）：
    CanonicalRequest =
        Method + "\\n" + CanonicalURI + "\\n" + CanonicalQuery + "\\n" +
        CanonicalHeaders + "\\n" + SignedHeaders + "\\n" + HexEncode(SHA256(Body))
    StringToSign = "SDK-HMAC-SHA256" + "\\n" + X-Sdk-Date + "\\n" +
        HexEncode(SHA256(CanonicalRequest))
    Authorization = "SDK-HMAC-SHA256 Access=<AK>, SignedHeaders=<...>, Signature=<...>"

注意 CanonicalURI 末尾必须补 "/"（华为云 CanonicalURI 规则，与 SDK 一致）。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests

from .const import EP_CHAT_V2, SNAP_HOST

SIGNING_ALGORITHM = "SDK-HMAC-SHA256"

# CanonicalURI / CanonicalQuery 的百分号编码保留集（RFC3986 unreserved）
_UNRESERVED = "-_.~"


@dataclass(frozen=True)
class SignCredential:
    """一次签名所需的凭证三元组。

    与 Go 版 `upstream.SignCredential` 字段一一对应。永久 AK/SK 模式下
    security_token 为空串。
    """

    access_key_id: str
    secret_access_key: str
    security_token: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)


def quote(value) -> str:
    """RFC3986 百分号编码（保留 unreserved 字符）。"""
    return urllib.parse.quote(str(value), safe=_UNRESERVED)


def canonical_uri(path: str) -> str:
    """规范 URI：逐段编码后补尾斜杠。

    逐段编码（而不是整串）是为了让路径里的 "/" 保持为分隔符；对齐 Go 的
    `canonicalURI()` 与 JS SDK 的 CanonicalURI。
    """
    segments = (path or "/").split("/")
    out = "/".join(quote(seg) for seg in segments)
    if not out.endswith("/"):
        out += "/"
    return out


def canonical_query(raw_query: str) -> str:
    """规范查询串：按 key、再按 value 排序，各自百分号编码。"""
    if not raw_query:
        return ""
    params = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    params.sort(key=lambda item: (item[0], item[1]))
    return "&".join(f"{quote(key)}={quote(value)}" for key, value in params)


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def sdk_date_now() -> str:
    """X-Sdk-Date：UTC 的 `%Y%m%dT%H%M%SZ`。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _canonical_headers(headers: dict) -> tuple:
    """把头部字典转成 (CanonicalHeaders, SignedHeaders)。

    头部名一律小写后排序（华为云规范），值只去首尾空白（不改内部空格）。
    """
    normalized = {str(name).lower(): value for name, value in headers.items()}
    names = sorted(normalized)
    canonical = "".join(f"{name}:{str(normalized[name]).strip()}\n" for name in names)
    return canonical, ";".join(names)


def _build_signature(
    method: str,
    uri: str,
    query: str,
    headers: dict,
    body: bytes,
    cred: SignCredential,
) -> str:
    """返回 Authorization 头的完整取值。"""
    canonical_headers, signed_headers = _canonical_headers(headers)
    payload_hash = sha256_hex(body)
    canonical_request = (
        f"{method.upper()}\n{uri}\n{query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"{SIGNING_ALGORITHM}\n{headers['x-sdk-date']}\n"
        f"{sha256_hex(canonical_request.encode('utf-8'))}"
    )
    signature = hmac_hex(cred.secret_access_key, string_to_sign)
    return (
        f"{SIGNING_ALGORITHM} Access={cred.access_key_id}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def signed_headers(
    method: str,
    url: str,
    body: bytes = b"",
    cred: Optional[SignCredential] = None,
    extra_headers: Optional[dict] = None,
) -> dict:
    """构造一个完整的、已签名的请求头字典（通用签名器）。

    与基线 `_signed_request()` 的头部集合保持一致：host / content-type / x-sdk-date
    + 调用方额外指定的头（如 agent-type / x-language）。有 security_token 时追加
    `x-security-token` —— 这是 OAuth 临时凭证的必需项。
    """
    cred = cred or SignCredential("", "")
    parsed = urllib.parse.urlparse(url)
    headers: dict = {}
    for key, value in (extra_headers or {}).items():
        headers[key.lower()] = str(value)
    headers["host"] = parsed.netloc
    headers.setdefault("content-type", "application/json")
    headers["x-sdk-date"] = sdk_date_now()
    if cred.security_token:
        headers["x-security-token"] = cred.security_token

    headers["Authorization"] = _build_signature(
        method,
        canonical_uri(parsed.path or "/"),
        canonical_query(parsed.query),
        headers,
        body,
        cred,
    )
    return headers


def sign_request(
    method: str,
    url: str,
    body: bytes = b"",
    cred: Optional[SignCredential] = None,
    extra_headers: Optional[dict] = None,
    timeout: int = 30,
    session: Optional[requests.Session] = None,
) -> requests.Response:
    """发送带签名的请求（通用签名器）。

    返回原始 Response，调用方自行判状态码：不同上游的「业务错误」表达方式不同
    （有的用 HTTP 状态码，有的用 200 + error_code），统一在这一层判定会丢信息。
    """
    headers = signed_headers(method, url, body, cred, extra_headers)
    agent = session or requests
    return agent.request(
        method.upper(), url, data=body or None, headers=headers, timeout=timeout
    )


def chat_headers(
    cred: SignCredential,
    body: bytes,
    benefit: bool = False,
    model_id: str = "",
    host: str = "",
    chat_path: str = EP_CHAT_V2,
) -> dict:
    """构造 `/api/v2/chat/completions` 的签名请求头。

    benefit=True 时按 IDE 免费模型通道附加 `maas_type` / `model-id` / `model-name`
    三个头，这三个头必须参与签名，否则上游返回 "The model is not registered"；
    老套餐模型带上这些头反而报 "unsupported model"，因此两条通道严格区分。

    OAuth（STS 临时凭证）模式额外补 `x-security-token` 与 `x-auth-token`：
    前者是华为云签名规范要求的临时凭证头，后者是 CodeArts 聊天网关的鉴权头
    （对齐 Go 版 ChatHeadersV2 的 `x-auth-token: <security_token>`）。永久 AK/SK
    模式下这两个头都不出现，保证与基线逐字节一致。
    """
    parsed = urllib.parse.urlparse(host or SNAP_HOST)
    headers = {
        "host": parsed.netloc,
        "content-type": "application/json",
        "x-sdk-date": sdk_date_now(),
    }
    if benefit and model_id:
        headers["maas_type"] = "benefit"
        headers["model-id"] = model_id
        headers["model-name"] = model_id
    if cred.security_token:
        headers["x-security-token"] = cred.security_token
        headers["x-auth-token"] = cred.security_token

    # CanonicalURI 固定带尾斜杠（抓包确认），查询串为空
    headers["Authorization"] = _build_signature(
        "POST", canonical_uri(chat_path), "", headers, body, cred
    )
    return headers


def json_body(payload) -> bytes:
    """紧凑序列化请求体。

    签名覆盖的是这些字节，因此调用方必须用返回的 bytes 发请求，不能再重新 dumps
    一次（键顺序/空格不同就会签名不符）。ensure_ascii=False 是为了中文内容按
    UTF-8 原样发送（与基线一致）。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# OAuth / PKCE 辅助
# ---------------------------------------------------------------------------

def random_hex(nbytes: int) -> str:
    """nbytes 字节的随机 hex（ticket_id / secret 用，对齐 Go 的 RandomHex）。"""
    return os.urandom(nbytes).hex()


def pkce_pair() -> tuple:
    """生成 PKCE code_verifier / code_challenge（S256）。

    verifier 用 64 字节随机再 base64url（对齐 Go 的 PKCE()），challenge 是
    verifier 的 SHA-256 再做 base64url 无填充。
    """
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge
