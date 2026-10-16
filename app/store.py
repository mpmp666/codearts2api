# -*- coding: utf-8 -*-
"""凭证与状态持久化。

三类文件，为什么要分开：
  * `auths/codearts-{user_id}.json` —— 账号凭证。**字段与 Go 版 `internal/auth/auth.go`
    逐字段对齐**，这样从 Go 版迁移过来的 auths 目录可以被直接读取（反之亦然），
    迁移成本为零。
  * `data/state.json` —— 账号池的运行时状态（错误计数 / 冷却截止 / 禁用原因）。
    与凭证分开是因为凭证是「事实」、状态是「运维结果」：重装/清状态不该逼用户重新登录。
  * `data/chats.json` —— 多轮上下文分组（会话 → chat_id / 账号黏性）。

永久 AK/SK（`.env` 里的 CODEARTS_AK/SK）**不落盘**：它本来就在 .env 里，再复制一份到
auths/ 只会扩大泄露面。它在内存里被合成为一个 uid=`apikey` 的凭证。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

from .atomic import write_json_atomic
from .signing import SignCredential

MODE_APIKEY = "apikey"
MODE_OAUTH = "oauth"

# .env 永久 AK/SK 合成的账号 uid（固定值，保证重启后池状态文件仍能对上）
APIKEY_UID = "apikey"
APIKEY_NAME = "永久 AK/SK 账号"


def _now_ts() -> int:
    return int(datetime.datetime.now().timestamp())


def _parse_rfc3339(value: str) -> Optional[datetime.datetime]:
    """解析 RFC3339（含 Go 的 RFC3339Nano 变体）。失败返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    # Go 的 RFC3339Nano 可能带 9 位小数秒，Python 只吃到 6 位，先截断
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[index:]
                break
        text = head + ("." + digits[:6] if digits else "") + rest
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


@dataclass
class Credential:
    """一个账号的凭证快照（永久 AK/SK 或 OAuth STS 临时凭证）。"""

    uid: str
    user_name: str = ""
    domain_id: str = ""
    mode: str = MODE_OAUTH
    access_key_id: str = ""
    secret_access_key: str = ""
    security_token: str = ""          # 对应 Go 的 cloud_dragon_token
    expiration: str = ""              # RFC3339
    refresh_token: str = ""
    code_verifier: str = ""           # PKCE verifier，刷新时需要
    client_id: str = ""
    dpop_private_key: dict = field(default_factory=dict)
    updated_at: int = 0
    path: str = ""                    # 落盘路径；空表示不持久化（.env AK/SK）

    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # --- 构造 ---

    @property
    def persistent(self) -> bool:
        return bool(self.path)

    @property
    def label(self) -> str:
        return self.user_name or self.uid

    def file_name(self) -> str:
        safe = (self.uid or "unknown").replace(os.sep, "_").replace("/", "_")
        return f"codearts-{safe}.json"

    # --- 签名用 ---

    def sign_credential(self) -> SignCredential:
        with self._lock:
            return SignCredential(
                access_key_id=self.access_key_id,
                secret_access_key=self.secret_access_key,
                security_token=self.security_token,
            )

    # --- 有效期 ---

    def expires_at(self) -> Optional[datetime.datetime]:
        with self._lock:
            expiration = self.expiration
        if self.mode == MODE_APIKEY:
            # 永久凭证没有过期时间
            return None
        return _parse_rfc3339(expiration)

    def remaining_seconds(self) -> float:
        """剩余有效期（秒）。

        永久 AK/SK 返回 +inf：它不会过期，调度器不该为它安排续期。无法解析的过期
        时间返回 0（当作已过期，触发一次刷新尝试）。
        """
        if self.mode == MODE_APIKEY:
            return float("inf")
        expires = self.expires_at()
        if expires is None:
            return 0.0
        now = datetime.datetime.now(datetime.timezone.utc)
        return (expires - now).total_seconds()

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def expiring_soon(self, within_seconds: float) -> bool:
        remaining = self.remaining_seconds()
        return 0 < remaining <= within_seconds

    def can_refresh(self) -> bool:
        with self._lock:
            return bool(self.refresh_token)

    def remaining_text(self) -> str:
        if self.mode == MODE_APIKEY:
            return "永久"
        remaining = self.remaining_seconds()
        if remaining <= 0:
            return "已过期"
        seconds = int(remaining)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        if hours:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"

    # --- 更新 ---

    def update_credentials(
        self,
        security_token: str,
        access_key_id: str,
        secret_access_key: str,
        expiration: str,
        refresh_token: str = "",
    ) -> None:
        """刷新后原子替换 STS 凭证并写回磁盘。

        refresh_token 轮转：上游每次刷新都会下发新的 refresh_token，必须写回，
        否则下一次刷新用的是已作废的旧值（上游会报 has been used）。
        """
        with self._lock:
            self.security_token = security_token
            self.access_key_id = access_key_id
            self.secret_access_key = secret_access_key
            self.expiration = expiration
            if refresh_token:
                self.refresh_token = refresh_token
            self.updated_at = _now_ts()
        self.save()

    def update_identity(self, uid: str = "", user_name: str = "", domain_id: str = "") -> None:
        with self._lock:
            if uid:
                self.uid = uid
            if user_name:
                self.user_name = user_name
            if domain_id:
                self.domain_id = domain_id
            self.updated_at = _now_ts()
        self.save()

    # --- 序列化 ---

    def to_dict(self) -> dict:
        with self._lock:
            data = {
                "user_id": self.uid,
                "user_name": self.user_name,
                "domain_id": self.domain_id,
                "cloud_dragon_token": self.security_token,
                "access_key_id": self.access_key_id,
                "secret_access_key": self.secret_access_key,
                "expiration": self.expiration,
                "refresh_token": self.refresh_token,
                "code_verifier": self.code_verifier,
                "updated_at": self.updated_at or _now_ts(),
                "mode": self.mode,
            }
            if self.client_id:
                data["client_id"] = self.client_id
            if self.dpop_private_key:
                data["dpop_private_key"] = dict(self.dpop_private_key)
            return data

    @classmethod
    def from_dict(cls, data: dict, path: str = "") -> "Credential":
        """从 auth 文件内容还原凭证。

        字段名同时兼容 Go 版（cloud_dragon_token）与可能的驼峰写法；mode 缺失时按
        「有 refresh_token 就是 oauth，否则是 apikey」推断，这样 Go 版导出的旧文件
        也能直接用。
        """
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in (MODE_APIKEY, MODE_OAUTH):
            mode = MODE_OAUTH if data.get("refresh_token") else MODE_APIKEY
        return cls(
            uid=str(data.get("user_id") or data.get("uid") or "").strip(),
            user_name=str(data.get("user_name") or data.get("nickname") or "").strip(),
            domain_id=str(data.get("domain_id") or "").strip(),
            mode=mode,
            access_key_id=str(data.get("access_key_id") or "").strip(),
            secret_access_key=str(data.get("secret_access_key") or "").strip(),
            security_token=str(data.get("cloud_dragon_token") or data.get("security_token") or "").strip(),
            expiration=str(data.get("expiration") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
            code_verifier=str(data.get("code_verifier") or "").strip(),
            client_id=str(data.get("client_id") or "").strip(),
            dpop_private_key=dict(data.get("dpop_private_key") or {}),
            updated_at=int(data.get("updated_at") or 0),
            path=path,
        )

    # --- 落盘 ---

    def save(self) -> None:
        """原子写回 auth 文件（0600）。

        没有 path（.env AK/SK 合成的账号）时静默跳过：绝不把 .env 的密钥复制到磁盘。
        """
        with self._lock:
            path = self.path
            payload = self.to_dict()
        if not path:
            return
        # 先写临时文件再 rename：中途崩溃不会留下半个 JSON 让下次启动读不出账号。
        # 走共享的原子写（唯一临时名 + 重试），避免并发保存互相踩临时文件。
        write_json_atomic(path, payload, mode=0o600)

    def set_path_and_save(self, path: str) -> None:
        with self._lock:
            self.path = path
        self.save()


def api_key_credential(ak: str, sk: str) -> Credential:
    """把 `.env` 的永久 AK/SK 合成为一个内存凭证（不落盘）。"""
    return Credential(
        uid=APIKEY_UID,
        user_name=APIKEY_NAME,
        mode=MODE_APIKEY,
        access_key_id=ak,
        secret_access_key=sk,
        updated_at=_now_ts(),
        path="",
    )


def load_auth_dir(auth_dir: str) -> list:
    """加载 auths 目录下的全部 codearts-*.json。

    坏文件跳过而不是抛异常：一个手工改坏的文件不该让整个服务起不来，日志里能看到
    文件名，运维自己决定删还是修。
    """
    out = []
    if not auth_dir or not os.path.isdir(auth_dir):
        return out
    for name in sorted(os.listdir(auth_dir)):
        if not name.startswith("codearts-") or not name.endswith(".json"):
            continue
        path = os.path.join(auth_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[store] 跳过无法解析的凭证 {name}: {error}")
            continue
        cred = Credential.from_dict(data, path=path)
        if not cred.access_key_id or not cred.secret_access_key:
            print(f"[store] 跳过缺少 AK/SK 的凭证 {name}")
            continue
        if not cred.uid:
            # 没有 user_id 时用文件名兜底，保证池里有稳定标识
            cred.uid = name[len("codearts-") : -len(".json")]
        out.append(cred)
    return out


def load_accounts(cfg) -> list:
    """按 mode 汇总可用账号。

    * `apikey` —— 只用 .env 的永久 AK/SK（基线行为）
    * `oauth`  —— 只用 auths/ 里的 OAuth 凭证
    * `auto`   —— 两者都用（默认；老用户升级后不用改配置就能同时用上）

    返回列表按 uid 排序，保证 WebUI/池顺序稳定。
    """
    accounts = []
    if cfg.mode in ("auto", MODE_OAUTH):
        accounts.extend(load_auth_dir(cfg.auth_dir))
    if cfg.mode in ("auto", MODE_APIKEY) and cfg.ak and cfg.sk:
        accounts.append(api_key_credential(cfg.ak, cfg.sk))
    accounts.sort(key=lambda item: item.uid)
    return accounts


def delete_auth_file(auth_dir: str, uid: str) -> bool:
    """删除某个账号的凭证文件。"""
    path = os.path.join(auth_dir, Credential(uid=uid).file_name())
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def fingerprint(value: str) -> str:
    """短指纹（日志/展示用，不泄露原值）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 池状态（data/state.json）
# ---------------------------------------------------------------------------

class StateStore:
    """账号池运行时状态的读写。

    与 Go 版 `internal/pool/pool.go` 的 saveState/loadState 字段对齐
    （name/err_count/cool_until/disabled/reason/last_error），并额外记录风控计数
    （risk_count）—— 风控退避是增强功能，Go 版没有。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        """返回 {name: entry}。文件缺失或损坏都返回空表（状态可丢，凭证不可丢）。"""
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[store] 状态文件无法解析，按空状态启动: {error}")
            return {}
        if isinstance(raw, dict):
            raw = raw.get("accounts") or []
        out = {}
        for entry in raw if isinstance(raw, list) else []:
            if isinstance(entry, dict) and entry.get("name"):
                out[entry["name"]] = entry
        return out

    def save(self, entries: list) -> None:
        if not self.path:
            return
        with self._lock:
            path = self.path
        write_json_atomic(path, {"accounts": entries}, mode=0o600)


# ---------------------------------------------------------------------------
# 多轮上下文（data/chats.json）
# ---------------------------------------------------------------------------

class ChatStore:
    """会话 → chat_id / 账号 的映射。

    为什么需要：OpenAI 线格式本身不带会话 ID，而 CodeArts 的 chat_id 决定内置 Agent
    的会话亲和与 prompt cache。这里把「显式 conversation_id / X-Codearts-Chat-Id」
    映射到具体账号，保证多轮请求粘在同一账号上（否则第二轮会被轮转丢到别的账号，
    上游看不到第一轮的上下文）。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.chats: dict = {}        # account_name -> 最近 chat_id
        self.conv_account: dict = {}  # conversation_id -> account_name
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.chats = dict(raw.get("chats") or {})
            self.conv_account = dict(raw.get("conv_account") or {})

    def save(self) -> None:
        if not self.path:
            return
        # 锁内只做快照：写盘可能因重试退避耗时，不该占着锁（remember() 是热路径）。
        with self._lock:
            path = self.path
            snapshot = {"chats": dict(self.chats), "conv_account": dict(self.conv_account)}
        write_json_atomic(path, snapshot)

    def account_for(self, conversation_id: str) -> str:
        if not conversation_id:
            return ""
        with self._lock:
            return self.conv_account.get(conversation_id, "")

    def remember(self, conversation_id: str, account_name: str, chat_id: str) -> None:
        if not conversation_id:
            return
        with self._lock:
            self.chats[account_name] = chat_id
            self.conv_account[conversation_id] = account_name
        self.save()
