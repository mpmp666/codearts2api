# -*- coding: utf-8 -*-
"""账号池：轮询 / 冷却 / 风控退避 / 并发上限 / token 校验与续期。

设计要点与「为什么」：

* **轮询**：按池内顺序挑第一个健康账号；失败的账号加入 tried 集合，本次请求不再选它，
  全部失败才报错。多账号不串台靠调用方的「会话 → 账号」黏性路由（见 server.py）。
* **冷却分三类**，因为它们的「正确反应」完全不同：
  - `ERR`  5xx 重试耗尽 → 冷却默认 300s（上游确实坏了，等一会儿再试）
  - `SOFT` 429 限流 → **不冷却**，原地等待后重试（限流是瞬时的，冷却反而把可用账号藏起来）
  - `RISK` 风控（3012 / 405 unusual activity）→ 指数退避（base 900s，上限 24h），
    累计 4 次升级为禁用（人工恢复）。风控是「调用太频繁」的信号，退避越久越安全，
    但不能无限退避，所以设 24h 上限；屡教不改说明这个账号已被重点盯上，直接停用更省事。
* **并发上限**：单账号默认 2（0=不限）。上游单账号并发会话上限实测只有 3 个且槽位释放
  很慢（>15s），所以默认留余量。满并发时**跳过该账号且不计入重试轮数**（SPEC 要求），
  但仍记入 tried 防止死循环。
* **鉴权失效（终态）与瞬时错误区别对待**：401 / refresh_token 终态失效 → 标记 invalid
  并禁用（需要人工重新登录）；网络超时/5xx → 只记错误计数，保持可调度。
"""
from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import requests

from . import checkin as checkin_mod
from . import oauth as oauth_mod
from .const import EP_CURRENT_USER, SNAP_HOST
from .signing import signed_headers
from .store import MODE_APIKEY, Credential, StateStore, delete_auth_file

# 校验结果的缓存时长：避免每个请求都去打一次刷新接口
VALIDATE_CACHE_SECONDS = 300.0


class CoolKind(Enum):
    """冷却原因（与 Go 版 CoolKind 对齐）。"""

    SOFT = "soft"
    ERR = "err"
    RISK = "risk"


@dataclass
class Account:
    """一个上游账号及其运行时状态。"""

    uid: str
    user_name: str = ""
    cred: Credential = None  # type: ignore[assignment]
    default_model: str = ""
    max_concurrent: int = 2
    enabled_at_start: bool = True

    # 运行时状态（由锁保护）
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    refresh_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    checkin_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    active_concurrent: int = 0
    last_validated: float = 0.0
    err_count: int = 0
    risk_count: int = 0
    cool_until: float = 0.0
    cool_kind: CoolKind = CoolKind.SOFT
    disabled: bool = False
    disabled_reason: str = ""
    last_err: str = ""
    invalid: bool = False
    keepalive_last_ping: float = field(default_factory=time.time)
    checkin: Optional[dict] = None
    # 可用额度快照（opengw /api/v1/user/tokens/balance 的结果）。
    # 与 checkin 分开：签到是「领取动作」，额度是「还剩多少」，两者刷新节奏不同。
    quota: Optional[dict] = None
    quota_fetched_at: float = 0.0

    @property
    def name(self) -> str:
        """账号名（等于 uid；与 Go 版 Account.Name 语义一致）。"""
        return self.uid

    @property
    def mode(self) -> str:
        return self.cred.mode if self.cred else MODE_APIKEY

    def label(self) -> str:
        return self.user_name or self.uid

    def cooling(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            return not self.disabled and now < self.cool_until

    def healthy(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            return not self.disabled and now >= self.cool_until

    def status_view(self) -> dict:
        """WebUI / 管理 API 用的状态快照（脱敏，不含任何密钥）。"""
        now = time.time()
        with self.lock:
            cooling = not self.disabled and now < self.cool_until
            remaining = max(0.0, self.cool_until - now)
            reason = self.disabled_reason or self.last_err
            view = {
                "uid": self.uid,
                "name": self.uid,
                "nickname": self.user_name,
                "user_name": self.user_name,
                "mode": self.mode,
                "default_model": self.default_model,
                "disabled": self.disabled,
                "invalid": self.invalid,
                "cooling": cooling,
                "until": _iso(self.cool_until) if cooling else "",
                "cool_remaining_seconds": int(remaining) if cooling else 0,
                "cool_kind": self.cool_kind.value,
                "err_count": self.err_count,
                "risk_count": self.risk_count,
                "reason": reason,
                "last_error": self.last_err,
                "active_concurrent": self.active_concurrent,
                "max_concurrent": self.max_concurrent,
                "checkin": self.checkin,
                "quota": self.quota or {},
                "quota_fetched_at": _iso(self.quota_fetched_at) if self.quota_fetched_at else "",
                "last_validated": _iso(self.last_validated) if self.last_validated else "",
                "keepalive_last_ping": _iso(self.keepalive_last_ping),
            }
        if self.cred is not None:
            view["token_remaining"] = self.cred.remaining_text()
            expires = self.cred.expires_at()
            view["expires_at"] = expires.isoformat() if expires else ""
        else:
            view["token_remaining"] = ""
            view["expires_at"] = ""
        return view


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


class PoolError(RuntimeError):
    """池操作错误。"""


class Pool:
    """账号池。"""

    def __init__(self, cfg, credentials: list, state_store: Optional[StateStore] = None):
        self.cfg = cfg
        self.state_store = state_store or StateStore(cfg.state_file)
        self._lock = threading.RLock()
        self._accounts: list = []
        self._by_uid: dict = {}
        for cred in credentials:
            self._add(cred)
        self._load_state()

    # ------------------------------------------------------------------
    # 账号集合
    # ------------------------------------------------------------------

    def _make_account(self, cred: Credential) -> Account:
        return Account(
            uid=cred.uid,
            user_name=cred.user_name,
            cred=cred,
            default_model=self.cfg.default_model,
            max_concurrent=self.cfg.max_concurrent,
        )

    def _add(self, cred: Credential) -> Account:
        account = self._make_account(cred)
        with self._lock:
            existing = self._by_uid.get(account.uid)
            if existing is not None:
                # 同一 uid 重新登录：换凭证并清掉旧的失败状态（用户刚修好它）
                with existing.lock:
                    existing.cred = cred
                    existing.user_name = cred.user_name
                    existing.disabled = False
                    existing.disabled_reason = ""
                    existing.invalid = False
                    existing.err_count = 0
                    existing.risk_count = 0
                    existing.cool_until = 0.0
                    existing.last_err = ""
                    existing.last_validated = 0.0
                return existing
            self._accounts.append(account)
            self._by_uid[account.uid] = account
            return account

    def add_credential(self, cred: Credential) -> Account:
        """动态添加账号（WebUI 登录成功后调用）。"""
        account = self._add(cred)
        self.save_state()
        return account

    def accounts(self) -> list:
        with self._lock:
            return list(self._accounts)

    def get(self, uid: str) -> Optional[Account]:
        with self._lock:
            return self._by_uid.get(uid)

    def uids(self) -> list:
        with self._lock:
            return [a.uid for a in self._accounts]

    def __len__(self) -> int:
        with self._lock:
            return len(self._accounts)

    # ------------------------------------------------------------------
    # 选号
    # ------------------------------------------------------------------

    def pick_excluding(self, tried) -> Optional[Account]:
        """挑一个健康账号（跳过 tried 里的）。

        顺序即轮询顺序：不额外随机化，方便排查「为什么总是这台」。多账号的负载均衡
        由「失败就换下一个」自然实现。
        """
        tried = tried or set()
        now = time.time()
        with self._lock:
            for account in self._accounts:
                if account.uid in tried:
                    continue
                if account.healthy(now):
                    return account
        return None

    def healthy_count(self) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for a in self._accounts if a.healthy(now))

    # ------------------------------------------------------------------
    # 并发槽位
    # ------------------------------------------------------------------

    def acquire(self, uid: str) -> bool:
        """尝试占用一个并发槽位。返回 False 表示已满（调用方应跳过该账号）。"""
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            if account.max_concurrent and account.active_concurrent >= account.max_concurrent:
                return False
            account.active_concurrent += 1
            account.keepalive_last_ping = time.time()
            return True

    def release(self, uid: str) -> None:
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            if account.active_concurrent > 0:
                account.active_concurrent -= 1

    def busy(self, uid: str) -> bool:
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            return account.active_concurrent > 0

    # ------------------------------------------------------------------
    # 状态变更
    # ------------------------------------------------------------------

    def cooldown(self, uid: str, kind: CoolKind, seconds: float, reason: str) -> None:
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            account.cool_until = time.time() + max(0.0, seconds)
            account.cool_kind = kind
            account.last_err = reason
        self.save_state()

    def note_error(self, uid: str, reason: str = "") -> None:
        """累计错误；达到阈值则按 ERR 冷却。"""
        account = self.get(uid)
        if account is None:
            return
        threshold = self.cfg.cooldown.err_threshold
        with account.lock:
            account.err_count += 1
            if reason:
                account.last_err = reason
            reached = account.err_count >= threshold
            if reached:
                account.cool_until = time.time() + self.cfg.cooldown.err_cooldown
                account.cool_kind = CoolKind.ERR
                account.last_err = reason or "consecutive errors"
        self.save_state()

    def note_success(self, uid: str) -> None:
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            account.err_count = 0

    def note_risk(self, uid: str, reason: str) -> None:
        """风控退避：指数增长，累计到阈值直接禁用。"""
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            account.risk_count += 1
            strikes = account.risk_count
            if strikes >= self.cfg.risk.max_strikes:
                account.disabled = True
                account.disabled_reason = (
                    f"风控累计 {strikes} 次，已停用（需人工确认后启用）: {reason}"
                )
                account.last_err = account.disabled_reason
                account.cool_until = 0.0
            else:
                seconds = min(
                    self.cfg.risk.base_seconds * (2 ** (strikes - 1)),
                    self.cfg.risk.max_seconds,
                )
                account.cool_until = time.time() + seconds
                account.cool_kind = CoolKind.RISK
                account.last_err = f"风控退避 {int(seconds)}s（第 {strikes} 次）: {reason}"
        self.save_state()

    def disable(self, uid: str, reason: str, *, invalid: bool = False) -> None:
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            account.disabled = True
            account.disabled_reason = reason
            account.last_err = reason
            if invalid:
                account.invalid = True
        self.save_state()

    def enable(self, uid: str) -> bool:
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            account.disabled = False
            account.disabled_reason = ""
            account.invalid = False
            account.err_count = 0
            account.risk_count = 0
            account.cool_until = 0.0
            account.last_err = ""
        self.save_state()
        return True

    def clear_cooldown(self, uid: str) -> bool:
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            account.cool_until = 0.0
            account.cool_kind = CoolKind.SOFT
        self.save_state()
        return True

    def delete(self, uid: str) -> bool:
        """删除账号：从池里移除并删掉凭证文件。

        .env 合成的永久 AK/SK 账号没有凭证文件，只能从池里移除（重启会再出现），
        这里显式返回 False 让调用方给出准确提示。
        """
        with self._lock:
            account = self._by_uid.pop(uid, None)
            if account is None:
                return False
            self._accounts = [a for a in self._accounts if a.uid != uid]
        removed = False
        if account.cred is not None and account.cred.path:
            removed = delete_auth_file(self.cfg.auth_dir, account.cred.uid)
        self.save_state()
        return removed

    def enabled(self, uid: str) -> bool:
        """账号是否启用（冷却中的账号仍算启用 —— 它还需要续期）。"""
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            return not account.disabled

    # ------------------------------------------------------------------
    # token 校验与续期
    # ------------------------------------------------------------------

    def validate(self, account: Account, *, force: bool = False) -> tuple:
        """校验账号可用性，必要时自动续期。

        返回 `(ok, error)`：
          * `(True, None)`  可用
          * `(False, None)` 终态失效（已禁用，需要重新登录）
          * `(False, err)`  瞬时错误（网络/5xx），账号保持可调度，等下次再试
        """
        if account is None:
            return False, PoolError("account not found")
        cred = account.cred
        with account.lock:
            if account.disabled and not force:
                return False, None
            fresh = (time.time() - account.last_validated) < VALIDATE_CACHE_SECONDS

        if cred.mode == MODE_APIKEY:
            # 永久 AK/SK 不会过期，也没有 refresh 通道
            with account.lock:
                account.last_validated = time.time()
            return True, None

        if fresh and not force:
            return True, None

        skew = self.cfg.watch.refresh_skew_minutes * 60
        if cred.expired() and not cred.can_refresh():
            self.disable(account.uid, "token 已过期且没有 refresh_token，需要重新登录", invalid=True)
            return False, None
        if cred.expiring_soon(skew) or cred.expired():
            if not cred.can_refresh():
                # ticket 通道不返回 refresh_token：未过期时只告警，到期后由上面分支处理
                print(f"[pool] 账号 {account.uid} token 即将过期但没有 refresh_token，需要重新登录")
                with account.lock:
                    account.last_validated = time.time()
                return True, None
            try:
                self.refresh(account)
            except oauth_mod.RefreshTokenExpiredError as error:
                self.disable(account.uid, f"refresh_token 失效，需要重新登录: {error}", invalid=True)
                return False, None
            except (oauth_mod.OAuthError, requests.RequestException) as error:
                # 网络/5xx 属于可重试错误：保持账号可调度，别把短暂故障固化成永久禁用
                with account.lock:
                    account.last_err = str(error)
                return False, error
        with account.lock:
            account.last_validated = time.time()
        return True, None

    def refresh(self, account: Account) -> None:
        """用 refresh_token 换新凭证（refresh_token 轮转后写回）。"""
        if account is None:
            raise PoolError("account not found")
        cred = account.cred
        with account.refresh_lock:
            if cred.mode == MODE_APIKEY:
                return
            if not cred.refresh_token:
                raise oauth_mod.OAuthError("没有 refresh_token，无法续期")
            if not cred.dpop_private_key:
                raise oauth_mod.OAuthError("缺少 DPoP 私钥，无法续期（需要重新登录）")
            login_cfg = oauth_mod.default_login_config(cred.client_id or self.cfg.login_client_id)
            result = oauth_mod.refresh_token(
                login_cfg, cred.refresh_token, cred.code_verifier, cred.dpop_private_key
            )
            cred.update_credentials(
                result.security_token,
                result.access_key_id,
                result.secret_access_key,
                result.expiration,
                result.refresh_token,
            )
            with account.lock:
                account.disabled = False
                account.disabled_reason = ""
                account.invalid = False
                account.last_err = ""
                account.last_validated = time.time()
            print(f"[pool] 账号 {account.uid} token 已续期，剩余 {cred.remaining_text()}")

    def refresh_if_needed(self, uid: str, skew_seconds: Optional[float] = None) -> bool:
        """剩余有效期低于 skew 时主动刷新。返回是否执行了刷新。"""
        account = self.get(uid)
        if account is None:
            raise PoolError(f"account not found: {uid}")
        skew = self.cfg.watch.refresh_skew_minutes * 60 if skew_seconds is None else skew_seconds
        cred = account.cred
        if cred.mode == MODE_APIKEY or not cred.can_refresh():
            return False
        if cred.remaining_seconds() > skew:
            return False
        self.refresh(account)
        return True

    # ------------------------------------------------------------------
    # 保活
    # ------------------------------------------------------------------

    def needs_keepalive(self, uid: str) -> bool:
        account = self.get(uid)
        if account is None:
            return False
        with account.lock:
            return (time.time() - account.keepalive_last_ping) > (
                self.cfg.watch.keepalive_window_minutes * 60
            )

    def ping_keepalive(self, uid: str) -> None:
        account = self.get(uid)
        if account is None:
            return
        with account.lock:
            account.keepalive_last_ping = time.time()

    def keepalive(self, uid: str, *, light_request: bool = True) -> dict:
        """保活：续期 + 一次轻量请求。

        轻量请求选 `GET /v1/current/user`（身份查询，无副作用、不计费）。它的意义是让
        上游看到这个会话还活着；失败只记日志**不处罚账号**，因为保活本身不是业务请求。
        唯一例外是 401：那说明凭证真的死了，交给 validate 走终态处理。
        """
        account = self.get(uid)
        if account is None:
            return {"uid": uid, "ok": False, "message": "account not found"}
        try:
            self.refresh_if_needed(uid)
        except oauth_mod.RefreshTokenExpiredError as error:
            self.disable(uid, f"refresh_token 失效: {error}", invalid=True)
            return {"uid": uid, "ok": False, "message": str(error)}
        except (oauth_mod.OAuthError, requests.RequestException) as error:
            return {"uid": uid, "ok": False, "message": f"续期失败（可重试）: {error}"}

        ok, error = self.validate(account, force=True)
        if not ok and error is not None:
            return {"uid": uid, "ok": False, "message": str(error)}
        if not ok:
            return {"uid": uid, "ok": False, "message": "凭证已失效，需要重新登录"}

        if light_request:
            self._light_request(account)
        self.ping_keepalive(uid)
        return {
            "uid": uid,
            "ok": True,
            "message": f"保活完成，token 剩余 {account.cred.remaining_text()}",
        }

    def _light_request(self, account: Account) -> None:
        """一次签名的轻量 GET（失败只记日志）。"""
        url = SNAP_HOST + EP_CURRENT_USER
        try:
            headers = signed_headers(
                "GET",
                url,
                b"",
                account.cred.sign_credential(),
                {"content-type": "application/json", "x-language": "zh-cn"},
            )
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code == 401:
                self.disable(account.uid, "保活请求返回 401，凭证已失效", invalid=True)
        except requests.RequestException as error:
            print(f"[pool] 账号 {account.uid} 保活轻量请求失败（忽略）: {error}")

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------

    def checkin(self, uid: str, timeout: int = 60) -> dict:
        """对一个账号执行签到（积分活动 + opengw 免费额度）。

        串行化同一账号的签到（checkin_lock）：claim/confirm 是两步操作，并发进来会出现
        「claim 成功、另一个线程又 claim 一次」的重复请求，上游虽然有 idempotentKey 兜底，
        但没必要制造这种竞争。
        """
        account = self.get(uid)
        if account is None:
            return {"uid": uid, "ok": False, "message": "account not found", "campaigns": []}
        with account.checkin_lock:
            with account.lock:
                if account.disabled:
                    return {
                        "uid": uid,
                        "ok": False,
                        "message": "账号已禁用，跳过签到",
                        "campaigns": [],
                    }
            ok, error = self.validate(account)
            if error is not None or not ok:
                message = str(error) if error is not None else "凭证无效，需要重新登录"
                self._store_checkin(account, ok=False, message=message)
                return {"uid": uid, "ok": False, "message": message, "campaigns": []}

            try:
                outcome = checkin_mod.checkin_account(
                    account.cred.sign_credential(), timeout=timeout
                )
                payload = outcome.to_dict()
            except Exception as error:
                # 兜底：签到链路的任何异常都只记录结果，绝不外抛、绝不改账号健康状态。
                # 这样即使 checkin 模块自己出 bug，也不会把管理 API 变成 500，
                # 更不会污染推理用的账号池。
                payload = {
                    "ok": False,
                    "credits": 0,
                    "message": f"签到异常: {error}",
                    "campaigns": [],
                    "benefit": {},
                }
            payload["uid"] = uid
            self._store_checkin(account, ok=bool(payload.get("ok")), message=payload.get("message") or "")
            # 签到刚联系过同一上游，顺手刷新额度快照（失败不影响签到结果）
            quota = self.refresh_quota(uid, force=True, timeout=timeout)
            if quota:
                payload["quota"] = quota
            return payload

    def _store_checkin(self, account: Account, ok: bool, message: str) -> None:
        """只记录签到结果。

        绝不调用 note_error / cooldown / disable：活动接口故障与账号能否推理无关，
        把它记进错误计数会让一次活动接口抖动冷却整池账号。
        """
        with account.lock:
            account.checkin = {
                "ok": bool(ok),
                "message": message,
                "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

    # ------------------------------------------------------------------
    # 可用额度
    # ------------------------------------------------------------------

    def refresh_quota(self, uid: str, *, ttl: float = 120.0,
                      timeout: int = 30, force: bool = False,
                      min_force_interval: float = 10.0) -> dict:
        """刷新某账号的可用额度快照（默认 2 分钟缓存）。

        **额度查询失败绝不处罚账号** —— 和签到一样，这是「运营信息」不是「推理
        健康」，一次余额接口抖动不该冷却整池。所以这里只记录结果、只返回，
        不碰 err_count / cool_until / disabled。

        两级节流，都是为了不打爆上游（上游对连续 billing/余额查询有风控信号）：
          * `ttl`：非强制调用走缓存（调度器 / 面板轮询）。
          * `min_force_interval`：**强制刷新也有地板**。用户连点「刷新」时
            每次点击都会强制绕过 ttl，没有地板的话点 10 下就是 10×N 次余额请求。
            地板内直接返回缓存（那次数字本来就是刚查的，语义上没错）。
        """
        account = self.get(uid)
        if account is None:
            return {}
        now = time.time()
        age = now - account.quota_fetched_at
        if account.quota is not None:
            if not force and age < ttl:
                return account.quota
            if force and age < min_force_interval:
                return account.quota
        try:
            quota = checkin_mod.fetch_quota(
                account.cred.sign_credential(), timeout=timeout
            )
        except Exception as error:  # noqa: BLE001 —— 额度查询失败不影响任何账号状态
            with account.lock:
                previous = dict(account.quota or {})
                previous["error"] = str(error)[:200]
            return previous
        with account.lock:
            account.quota = quota
            account.quota_fetched_at = now
        return quota

    def refresh_all_quotas(self, *, ttl: float = 120.0, timeout: int = 30,
                           force: bool = False) -> dict:
        """刷新全部启用账号的额度（给调度器 / 面板「刷新」/「校验账号」用）。"""
        out = {}
        for account in self.accounts():
            with account.lock:
                if account.disabled:
                    continue
            out[account.uid] = self.refresh_quota(
                account.uid, ttl=ttl, timeout=timeout, force=force
            )
        return out

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        now = time.time()
        total = healthy = disabled = cooling = 0
        for account in self.accounts():
            total += 1
            with account.lock:
                if account.disabled:
                    disabled += 1
                elif now < account.cool_until:
                    cooling += 1
                else:
                    healthy += 1
        return {
            "total": total,
            "healthy": healthy,
            "disabled": disabled,
            "cooling": cooling,
            "credits": healthy,  # 保留 Go 版的 credits 字段名，语义是健康账号数
        }

    def list_view(self) -> list:
        views = [account.status_view() for account in self.accounts()]
        views.sort(key=lambda item: item.get("uid") or "")
        return views

    # ------------------------------------------------------------------
    # 重载 / 状态持久化
    # ------------------------------------------------------------------

    def sync(self, credentials: list) -> dict:
        """用磁盘凭证全量对齐（重载 auths）。返回变更摘要。"""
        by_uid = {cred.uid: cred for cred in credentials}
        added, updated, removed = [], [], []
        with self._lock:
            existing = {a.uid: a for a in self._accounts}
            for uid, cred in by_uid.items():
                account = existing.get(uid)
                if account is None:
                    self._add(cred)
                    added.append(uid)
                else:
                    with account.lock:
                        account.cred = cred
                        account.user_name = cred.user_name
                    updated.append(uid)
            for uid in list(existing):
                if uid not in by_uid:
                    self._accounts = [a for a in self._accounts if a.uid != uid]
                    self._by_uid.pop(uid, None)
                    removed.append(uid)
        self.save_state()
        return {"added": added, "updated": updated, "removed": removed}

    def _load_state(self) -> None:
        entries = self.state_store.load()
        if not entries:
            return
        for account in self.accounts():
            entry = entries.get(account.uid)
            if not entry:
                continue
            with account.lock:
                account.err_count = int(entry.get("err_count") or 0)
                account.risk_count = int(entry.get("risk_count") or 0)
                account.cool_until = float(entry.get("cool_until") or 0)
                account.disabled = bool(entry.get("disabled"))
                account.disabled_reason = str(entry.get("reason") or "")
                account.last_err = str(entry.get("last_error") or "")
                account.invalid = bool(entry.get("invalid"))
                kind = str(entry.get("cool_kind") or "soft")
                account.cool_kind = {
                    "soft": CoolKind.SOFT,
                    "err": CoolKind.ERR,
                    "risk": CoolKind.RISK,
                }.get(kind, CoolKind.SOFT)

    def save_state(self) -> None:
        entries = []
        for account in self.accounts():
            with account.lock:
                entries.append(
                    {
                        "name": account.uid,
                        "err_count": account.err_count,
                        "risk_count": account.risk_count,
                        "cool_until": account.cool_until,
                        "cool_kind": account.cool_kind.value,
                        "disabled": account.disabled,
                        "invalid": account.invalid,
                        "reason": account.disabled_reason,
                        "last_error": account.last_err,
                    }
                )
        self.state_store.save(entries)
