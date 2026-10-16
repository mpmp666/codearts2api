# -*- coding: utf-8 -*-
"""华为云 CodeArts OAuth2（PKCE + DPoP）登录。

三条通道（对齐 Go 版 `cmd/login` 与 `internal/server/oauth.go`）：

1. **本机回调**（`python login.py`）：本地起 127.0.0.1 随机端口，自动开浏览器，
   portal 回调带 code → 换 STS 临时凭证。
2. **--print-only**（服务器无浏览器）：只打印授权链接，纯 **ticket 轮询**。
3. **WebUI 发起**：`/admin/api/oauth/start` 拿链接，浏览器登录后回调到本服务
   `/oauth/callback`；若浏览器在别的机器上（回调打不到本服务），可以把地址栏里的
   `http://127.0.0.1:xxxx/oauth/callback?...` **粘贴**进 `/admin/api/oauth/import-callback` 中转。

关键契约（照 Go 版实现，不要自创）：

* portal 会**回调两次**：第一次带 `secret`+`redirect`（必须 307 跳回 redirect，否则
  登录链路中断，带 code 的第二次回调永远不会到达）；第二次带 `code`。
* ticket 轮询必须用 **portal 下发的 secret**（本地生成的那份上游不认），拿不到才回退本地。
* 换到的 **STS 临时凭证**包含 `security_token` + `access_key_id` + `secret_access_key`
  + `refresh_token`；`refresh_token` 与 `client_id`、**DPoP 私钥**三者绑定。
* **DPoP 私钥必须与 refresh_token 一起持久化**并在刷新时原样复用，换密钥刷新会被
  STS 拒（`invalid refresh token: InvalidDPoPHeader`）。
* 登录超时 5 分钟。
"""
from __future__ import annotations

import datetime
import http.server
import json
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from . import dpop
from .const import (
    CLIENT_ID,
    EP_CALLER_IDENTITY,
    EP_CURRENT_USER,
    EP_LOGIN_TICKET,
    EP_OAUTH_TOKENS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    PORTAL_HOST,
    SNAP_HOST,
    SNAP_MANAGER_HOST,
    STS_HOST,
)
from .signing import SignCredential, pkce_pair, random_hex, signed_headers

# 登录超时：与 Go 版一致（5 分钟）
LOGIN_TIMEOUT_SECONDS = 300.0
# WebUI 会话有效期：比登录超时略长，允许用户慢慢操作
SESSION_TTL_SECONDS = 900.0
# ticket 轮询间隔
POLL_INTERVAL_SECONDS = 2.0


class OAuthError(RuntimeError):
    """OAuth 流程错误。"""


class RefreshTokenExpiredError(OAuthError):
    """refresh_token 已终态失效，必须重新登录。

    与网络/5xx 等可重试错误分开：继续重试没有意义，账号应被标记为 invalid 等人工介入，
    而不是每次 watchdog 都白打一次上游。
    """


@dataclass
class LoginConfig:
    """登录相关端点配置。"""

    client_id: str = CLIENT_ID
    portal_host: str = PORTAL_HOST
    snap_manager: str = SNAP_MANAGER_HOST
    sts_host: str = STS_HOST
    redirect_path: str = "/oauth/callback"
    plugin_name: str = PLUGIN_NAME
    plugin_version: str = PLUGIN_VERSION


def default_login_config(client_id: str = "") -> LoginConfig:
    cfg = LoginConfig()
    if client_id:
        cfg.client_id = client_id
    return cfg


@dataclass
class TokenResult:
    """oauth2/tokens 的归一化结果。"""

    user_id: str = ""
    user_name: str = ""
    domain_id: str = ""
    refresh_token: str = ""
    security_token: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    expiration: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.security_token and self.access_key_id and self.secret_access_key)

    def sign_credential(self) -> SignCredential:
        return SignCredential(self.access_key_id, self.secret_access_key, self.security_token)


# ---------------------------------------------------------------------------
# 基础请求
# ---------------------------------------------------------------------------

def build_authorize_url(
    cfg: LoginConfig, ticket_id: str, code_challenge: str, port: int
) -> str:
    """构造 portal 授权链接。

    `code_challenge_method` 必须是 `SHA-256`（不是 OAuth 常见的 `S256`）——
    portal 只识别官方插件实际使用的写法，照 Go 版抄。
    """
    params = {
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": cfg.client_id,
        "client_id": cfg.client_id,
        "port": str(port),
        "code_challenge": code_challenge,
        "code_challenge_method": "SHA-256",
        "ticket_id": ticket_id,
        "plugin-name": cfg.plugin_name,
        "plugin-version": cfg.plugin_version,
    }
    # 按 key 排序拼接，与 Go 的 url.Values.Encode() 一致（便于对照排查）
    query = urllib.parse.urlencode(sorted(params.items()))
    return f"{cfg.portal_host}/authorize?{query}"


def _request_token(
    cfg: LoginConfig,
    form: dict,
    dpop_jwk: dict,
    timeout: int = 30,
) -> TokenResult:
    """向 STS 令牌端点发 form + DPoP 证明。

    DPoP proof 的 htu 必须是**不带 query** 的完整 token 端点地址。
    """
    url = cfg.sts_host + EP_OAUTH_TOKENS
    try:
        proof = dpop.sign_proof(dpop_jwk, url)
    except dpop.DPoPError as error:
        raise OAuthError(f"生成 DPoP 证明失败: {error}") from error

    try:
        response = requests.post(
            url,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "DPoP": proof,
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise OAuthError(f"令牌端点不可达: {error}") from error

    raw = response.text or ""
    if response.status_code >= 400:
        payload = _safe_json(raw)
        if form.get("grant_type") == "refresh_token":
            # 刷新通道的「终态失效」单独识别：这些错误重试多少次都一样
            error_code = str(payload.get("error_code") or "")
            error_name = str(payload.get("error") or "")
            message = str(payload.get("error_msg") or "")
            terminal = (
                error_name == "invalid_grant"
                or "ExpiredRefreshToken" in error_code
                or "InvalidDPoPHeader" in error_code
                or "invalid client id" in error_code
                or "invalid client id" in message
                or "has been used" in message.lower()
            )
            if terminal:
                raise RefreshTokenExpiredError(
                    "refresh_token 已失效或被拒（需要重新登录）: "
                    f"http={response.status_code} error={error_name} "
                    f"code={error_code} msg={message[:160]}"
                )
        raise OAuthError(f"令牌端点返回 {response.status_code}: {raw[:300]}")

    payload = _safe_json(raw)
    result = TokenResult(
        user_id=str(payload.get("user_id") or ""),
        user_name=str(payload.get("user_name") or ""),
        domain_id=str(payload.get("domain_id") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
    )
    credentials = payload.get("credentials") or {}
    if credentials.get("security_token"):
        result.security_token = str(credentials.get("security_token") or "")
        result.access_key_id = str(credentials.get("access_key_id") or "")
        result.secret_access_key = str(credentials.get("secret_access_key") or "")
        result.expiration = str(credentials.get("expiration") or "")
    else:
        # 兼容 ticket / 旧通道字段（legacy）
        legacy = payload.get("credential") or {}
        if legacy.get("securitytoken"):
            result.security_token = str(legacy.get("securitytoken") or "")
            result.access_key_id = str(legacy.get("access") or "")
            result.secret_access_key = str(legacy.get("secret") or "")
            result.expiration = str(legacy.get("expires_at") or "")
    if not result.usable:
        raise OAuthError(f"令牌响应缺少凭证字段: {raw[:200]}")
    return result


def _safe_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def exchange_code(
    cfg: LoginConfig,
    code: str,
    code_verifier: str,
    port: int,
    dpop_jwk: dict,
    timeout: int = 30,
) -> TokenResult:
    """用授权码换 STS 临时凭证（authorization_code）。"""
    return _request_token(
        cfg,
        {
            "client_id": cfg.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": f"http://127.0.0.1:{port}{cfg.redirect_path}",
        },
        dpop_jwk,
        timeout,
    )


def refresh_token(
    cfg: LoginConfig,
    refresh: str,
    code_verifier: str,
    dpop_jwk: dict,
    timeout: int = 30,
) -> TokenResult:
    """用 refresh_token 换新凭证（refresh_token 轮转）。

    必须传签发该 refresh_token 时使用的 DPoP 私钥；传别的密钥会被 STS 拒。
    """
    if not dpop.has_private_key(dpop_jwk):
        raise OAuthError("缺少 DPoP 私钥，无法刷新（需要重新登录）")
    return _request_token(
        cfg,
        {
            "client_id": cfg.client_id,
            "code_verifier": code_verifier,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        dpop_jwk,
        timeout,
    )


def poll_ticket(cfg: LoginConfig, ticket_id: str, secret: str, timeout: int = 15) -> Optional[TokenResult]:
    """轮询登录结果（兜底通道）。未完成返回 None。

    注意：这个接口**不签名**，只带 plugin-name / plugin-version 头。
    """
    url = (
        f"{cfg.snap_manager}{EP_LOGIN_TICKET}"
        f"?ticket_id={urllib.parse.quote(ticket_id)}&secret={urllib.parse.quote(secret)}"
    )
    try:
        response = requests.get(
            url,
            headers={
                "plugin-name": cfg.plugin_name,
                "plugin-version": cfg.plugin_version,
            },
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    payload = _safe_json(response.text)
    if not payload:
        return None
    result = TokenResult(
        user_id=str(payload.get("user_id") or ""),
        user_name=str(payload.get("user_name") or ""),
        domain_id=str(payload.get("domain_id") or ""),
        refresh_token=str(payload.get("refresh_token") or ""),
    )
    credentials = payload.get("credentials") or {}
    if credentials.get("security_token"):
        result.security_token = str(credentials.get("security_token") or "")
        result.access_key_id = str(credentials.get("access_key_id") or "")
        result.secret_access_key = str(credentials.get("secret_access_key") or "")
        result.expiration = str(credentials.get("expiration") or "")
    else:
        legacy = payload.get("credential") or {}
        if legacy.get("securitytoken"):
            result.security_token = str(legacy.get("securitytoken") or "")
            result.access_key_id = str(legacy.get("access") or "")
            result.secret_access_key = str(legacy.get("secret") or "")
            result.expiration = str(legacy.get("expires_at") or "")
    # ticket 通道没有 user_name 时视为「还没登录完成」，与 Go 版判定一致
    if not result.user_name:
        return None
    return result


# ---------------------------------------------------------------------------
# 身份回填
# ---------------------------------------------------------------------------

def lookup_identity(cred: SignCredential, timeout: int = 20) -> tuple:
    """用 STS 凭证查账号身份：先 caller-identity，再 current/user。

    为什么必须做：portal 回调只回传 code，不带身份；user_id 为空会让凭证文件名退化成
    codearts-unknown.json，账号池按 uid 建索引就会加载不到（或与既有账号冲突）。
    """
    try:
        headers = signed_headers(
            "GET",
            STS_HOST + EP_CALLER_IDENTITY,
            b"",
            cred,
            {"content-type": "application/json", "x-language": "zh-cn"},
        )
        response = requests.get(STS_HOST + EP_CALLER_IDENTITY, headers=headers, timeout=timeout)
        if response.status_code < 400:
            data = _safe_json(response.text)
            urn = str(data.get("principal_urn") or "")
            name = urn.rsplit(":user:", 1)[-1] if ":user:" in urn else ""
            uid = str(data.get("principal_id") or "")
            if uid:
                return uid, name, str(data.get("account_id") or "")
    except requests.RequestException:
        pass

    try:
        url = SNAP_HOST + EP_CURRENT_USER
        headers = signed_headers(
            "GET", url, b"", cred, {"content-type": "application/json", "x-language": "zh-cn"}
        )
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code < 400:
            data = _safe_json(response.text)
            uid = str(data.get("user_id") or "")
            if uid:
                return uid, str(data.get("user_name") or ""), str(data.get("domain_id") or "")
    except requests.RequestException:
        pass
    return "", "", ""


def identity_from_refresh_token(token: str) -> str:
    """从 JWT 形式的 refresh_token 里取 sub 作为兜底 user_id。

    兜底路径的意义：身份接口偶发不可用时也要保证 user_id 有值，否则会写出
    codearts-unknown.json，账号池按 uid 索引就找不到这个账号。
    """
    import base64

    parts = (token or "").split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return ""
    return str(data.get("sub") or "") if isinstance(data, dict) else ""


# ---------------------------------------------------------------------------
# 回调状态：portal 首次回调下发的 secret
# ---------------------------------------------------------------------------

class CallbackState:
    """保存 portal 回调里下发的 secret。

    portal 首次回调只带 secret + redirect（不含 code），而轮询 `/v1/login/ticket`
    必须用 portal 下发的 secret（本地生成的那份上游不认），所以两边都要用收到的值。
    """

    def __init__(self, fallback_secret: str):
        self._lock = threading.Lock()
        self._secret = ""
        self._fallback = fallback_secret
        self.code = ""
        self.redirect = ""

    def set_secret(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            self._secret = value

    def poll_secret(self) -> str:
        with self._lock:
            return self._secret or self._fallback


# ---------------------------------------------------------------------------
# 本地回调服务器（本机登录通道）
# ---------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """处理 portal 的两次回调。

    第一次（secret + redirect）必须 307 跳回 redirect —— 这是登录页链路的一环，
    在这里报错会中断链路，带 code 的第二次回调永远不会到达。
    """

    state: CallbackState = None  # type: ignore[assignment]
    redirect_path = "/oauth/callback"
    on_code: Callable[[str], None] = None  # type: ignore[assignment]

    def log_message(self, *args):  # 静音默认 access log，避免污染 CLI 输出
        return

    def _handle(self, query: dict, body: str = "") -> None:
        code = (query.get("code") or [""])[0]
        secret = (query.get("secret") or [""])[0]
        redirect = (query.get("redirect") or [""])[0]
        if secret:
            self.state.set_secret(secret)
        if not code and redirect:
            self.send_response(307)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        if not code and body:
            # 兼容 POST body 传 code
            code = (urllib.parse.parse_qs(body).get("code") or [""])[0]
        self.send_response(200 if code else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if not code:
            self.wfile.write("<h3>登录失败：缺少 code</h3>".encode("utf-8"))
            return
        self.wfile.write("<h3>登录成功，可关闭此页面。</h3>".encode("utf-8"))
        self.on_code(code)

    def do_GET(self):  # noqa: N802 (http.server 接口命名)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.redirect_path:
            self.send_response(404)
            self.end_headers()
            return
        self._handle(urllib.parse.parse_qs(parsed.query))

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        self._handle(urllib.parse.parse_qs(parsed.query), body)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def open_browser(url: str) -> bool:
    """尽力打开浏览器；失败不算错误（服务器上没有浏览器是常态）。"""
    try:
        return webbrowser.open(url)
    except Exception:  # webbrowser 在不同平台可能抛各种异常
        return False


def login_interactive(
    cfg: LoginConfig,
    *,
    print_only: bool = False,
    on_status: Callable[[str], None] = print,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> tuple:
    """执行一次完整登录。

    返回 `(TokenResult, dpop_jwk, code_verifier)`；调用方负责落盘（必须把
    dpop_jwk 与 refresh_token 一起保存）。

    双通道：本地回调拿到 code 走 authorization_code；拿不到就走 ticket 轮询
    （print_only 模式只有这一条路）。
    """
    verifier, challenge = pkce_pair()
    ticket_id = random_hex(16)
    local_secret = random_hex(16)
    dpop_jwk = dpop.new_private_jwk()
    state = CallbackState(local_secret)
    code_holder: list = []
    code_event = threading.Event()

    server = None
    port = 0
    if not print_only:
        port = _free_port()

        class Handler(_CallbackHandler):
            pass

        Handler.state = state
        Handler.redirect_path = cfg.redirect_path
        Handler.on_code = lambda code: (code_holder.append(code), code_event.set())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    auth_url = build_authorize_url(cfg, ticket_id, challenge, port)

    on_status("=" * 60)
    on_status("  CodeArts Agent 登录（华为云账号）")
    on_status("=" * 60)
    on_status("步骤：")
    on_status("  1. 打开下面链接，用华为云账号完成登录")
    on_status("  2. 登录成功后浏览器会跳回 127.0.0.1（服务器模式跳转失败可忽略）")
    on_status("  3. 本工具自动换取 STS 临时凭证并落盘 auths/")
    on_status("")
    on_status("登录链接：")
    on_status("  " + auth_url)
    on_status("")

    if not print_only:
        open_browser(auth_url)

    deadline = time.time() + timeout
    result: Optional[TokenResult] = None
    last_error = ""
    try:
        while time.time() < deadline:
            if code_event.wait(POLL_INTERVAL_SECONDS):
                code = code_holder[0] if code_holder else ""
                if code:
                    try:
                        result = exchange_code(cfg, code, verifier, port, dpop_jwk)
                        break
                    except OAuthError as error:
                        last_error = str(error)
                        on_status(f"授权码换取凭证失败: {error}")
            # ticket 轮询用 portal 下发的 secret，拿不到才回退本地生成的那份
            ticket = poll_ticket(cfg, ticket_id, state.poll_secret())
            if ticket is not None:
                result = ticket
                break
        else:
            minutes = max(1, int(round(timeout / 60)))
            raise OAuthError(f"登录超时（{minutes} 分钟），请重新执行")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    if result is None:
        raise OAuthError(last_error or "登录未完成")
    return result, dpop_jwk, verifier


# ---------------------------------------------------------------------------
# WebUI 用的 OAuth 会话
# ---------------------------------------------------------------------------

@dataclass
class OAuthSession:
    """一次 WebUI 发起的授权会话。"""

    session_id: str
    ticket_id: str
    secret: str
    verifier: str
    dpop_private_key: dict
    port: int
    auth_url: str
    created_at: float = field(default_factory=time.time)
    done: bool = False
    error: str = ""
    ticket_fallback: bool = False      # portal 是否已下发 secret（决定能否走 ticket 轮询）
    account_uid: str = ""
    account_name: str = ""


class OAuthSessionStore:
    """WebUI OAuth 会话表（内存 + TTL 回收）。

    为什么只在内存：会话里含 PKCE verifier 与 DPoP 私钥，落盘等于多一份泄露面；
    服务重启后旧会话失效是**可接受**的（WebUI 会提示重新发起授权）。注意这带来一个
    已知限制：授权链接生成后若服务重启，带 code 的回调会因 PKCE verifier 丢失而失败，
    错误信息里会明确提示重新发起。
    """

    def __init__(self, ttl: float = SESSION_TTL_SECONDS):
        self._lock = threading.Lock()
        self._sessions: dict = {}
        self.ttl = ttl

    def _gc_locked(self) -> None:
        now = time.time()
        for key in [k for k, s in self._sessions.items() if now - s.created_at > self.ttl]:
            self._sessions.pop(key, None)

    def put(self, session: OAuthSession) -> None:
        with self._lock:
            self._gc_locked()
            self._sessions[session.session_id] = session

    def get(self, session_id: str) -> Optional[OAuthSession]:
        with self._lock:
            self._gc_locked()
            return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def complete(self, session_id: str) -> None:
        session = self.get(session_id)
        if session is not None:
            session.done = True
            session.error = ""

    def enable_ticket_fallback(self, *, session_id: str = "", ticket_id: str = "", secret: str) -> bool:
        """记录 portal 下发的 secret 并开启 ticket 兜底通道。"""
        with self._lock:
            self._gc_locked()
            for session in self._sessions.values():
                if (session_id and session.session_id == session_id) or (
                    ticket_id and session.ticket_id == ticket_id
                ):
                    session.secret = secret
                    session.ticket_fallback = True
                    return True
        return False

    def code_candidates(self, ticket_id: str = "", secret: str = "") -> list:
        """返回授权码可能属于的会话（按可信度排序）。

        实测 portal 第二次回调**只带 code**（既没有 secret 也没有 ticket_id），
        因此无法从回调本身确定配对，只能按下列顺序给出候选：

          1. secret 命中（部分部署会带；带了但不命中说明会话已失效，直接放弃）
          2. ticket_id 命中
          3. 都没有判别键时，返回全部活跃会话（按创建时间倒序）

        第 3 种由调用方逐个尝试换取：授权码与 PKCE verifier 一对一校验，用错只会得到
        STS5.1805 且**不消耗授权码**，所以逐个试是安全的；而只挑其中一个在服务重启过
        或同时开过两次登录时必然错配。
        """
        with self._lock:
            self._gc_locked()
            active = [s for s in self._sessions.values() if not s.done]
            if secret:
                for session in active:
                    if session.secret == secret:
                        return [session]
                return []
            if ticket_id:
                for session in active:
                    if session.ticket_id == ticket_id:
                        return [session]
                return []
            active.sort(key=lambda s: s.created_at, reverse=True)
            return active

    def active_count(self) -> int:
        with self._lock:
            self._gc_locked()
            return len(self._sessions)


def start_session(
    store: OAuthSessionStore,
    cfg: LoginConfig,
    listen_port: int,
    *,
    callback_host: str = "",
) -> OAuthSession:
    """发起一次 WebUI 授权会话，返回含 auth_url 的会话对象。"""
    ticket_id = random_hex(16)
    secret = random_hex(16)
    verifier, challenge = pkce_pair()
    dpop_jwk = dpop.new_private_jwk()
    auth_url = build_authorize_url(cfg, ticket_id, challenge, listen_port)
    if callback_host:
        # 远端部署时把回调端口改写成反代端口，让浏览器回调尽量命中本服务；
        # 远端仍打不通时会回退 ticket 轮询通道，不影响登录完成。
        port = _port_of_callback_host(callback_host)
        if port:
            auth_url = _rewrite_auth_url_port(auth_url, port)

    session = OAuthSession(
        session_id=random_hex(16),
        ticket_id=ticket_id,
        secret=secret,
        verifier=verifier,
        dpop_private_key=dpop_jwk,
        port=listen_port,
        auth_url=auth_url,
    )
    store.put(session)
    return session


def _port_of_callback_host(host: str) -> int:
    """从 oauth_callback_host 里取出**显式写出**的端口，没有则返回 0。

    只认显式端口、不按 scheme 推断 443/80：portal 的回调目标是
    127.0.0.1:{port}，若在没写端口时擅自改成 443，本机浏览器的回调反而会失败。
    """
    text = (host or "").strip()
    if not text:
        return 0
    parsed = urllib.parse.urlparse(text if "//" in text else "//" + text)
    if parsed.port:
        return parsed.port
    if ":" in parsed.netloc:
        tail = parsed.netloc.rsplit(":", 1)[-1].strip("/")
        if tail.isdigit():
            return int(tail)
    return 0


def _rewrite_auth_url_port(auth_url: str, port: int) -> str:
    """改写授权链接的 port 参数（portal 据此拼回调地址）。"""
    parsed = urllib.parse.urlparse(auth_url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query["port"] = [str(port)]
    flat = sorted((key, value) for key, values in query.items() for value in values)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(flat)))


def validate_callback_url(
    callback_url: str, session: OAuthSession
) -> tuple:
    """校验粘贴进来的回调地址，返回 (是否合法, 错误信息, 解析结果)。

    只接受华为返回的 127.0.0.1/localhost 回调，且端口必须与当前会话一致 ——
    这是防止有人把任意 URL 塞进来触发服务端请求（SSRF）的第一道闸。
    """
    parsed = urllib.parse.urlparse((callback_url or "").strip())
    if parsed.scheme != "http":
        return False, "只接受 http:// 的本地回调地址", parsed
    if parsed.username or parsed.password:
        return False, "回调地址不能带用户信息", parsed
    if parsed.path.rstrip("/") != "/oauth/callback":
        return False, "回调地址路径必须是 /oauth/callback", parsed
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in ("localhost", "127.0.0.1", "::1", "[::1]"):
        return False, "只接受华为返回的 127.0.0.1 OAuth 回调地址", parsed
    if str(parsed.port or "") != str(session.port):
        return False, f"回调端口与当前授权会话不匹配（期望 {session.port}）", parsed
    return True, "", parsed


def validate_redirect_url(redirect: str, session: OAuthSession) -> bool:
    """校验首次回调里的 redirect 是否属于本会话。

    必须校验：这个值会被 307 出去，不校验等于开放重定向。
    """
    parsed = urllib.parse.urlparse(redirect or "")
    portal = urllib.parse.urlparse(PORTAL_HOST)
    query = urllib.parse.parse_qs(parsed.query)
    return (
        parsed.scheme == "https"
        and not parsed.username
        and parsed.hostname == portal.hostname
        and (query.get("ticket_id") or [""])[0] == session.ticket_id
    )


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
