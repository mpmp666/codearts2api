# -*- coding: utf-8 -*-
"""签到：两套并存。

**A. 积分活动（Go 版的，本实现新增）** —— 对齐 `ca2a/internal/upstream/checkin.go`：

    GET  /v1/ops/delivery?channel=IDE      查活动列表
    POST /v1/ops/claim                     {campaignId, channel:"IDE", idempotentKey}
    POST /v1/ops/confirm                   {campaignId}

只处理 `campaignId == 1`（每日签到）与 `== 4`（新用户），`benefitUnit` 非空且不等于
`CREDIT` 的跳过。状态机是本模块的核心：

    CONFIRMED / CONSUMED → 已领取，跳过（OK=true，不算失败）
    CLAIMED              → 已到账未确认 → **只补 confirm，绝不重复 claim**
    ELIGIBLE             → claimable 为假则报「暂不可领取」；否则 claim → 再 confirm
    ""                   → 未获得活动资格
    其他                 → 未知状态（OK=false，让面板显示出来而不是静默吞掉）

活动起止时间（`extra.startTime` / `extra.endTime`，RFC3339Nano）**先校验**：活动结束后
再 claim 只会拿到业务错误，白打一次上游还容易让人误判账号有问题。

**B. opengw 免费额度签到（基线已有，保留）**：
`opengw.developer.huaweicloud.com/api/v1/benefit/claim`（GET 状态 / POST 领取）
+ `/api/v1/user/tokens/balance` + `/api/v1/gateway/config`。

**为什么签到故障绝不能计错误/冷却**：活动接口挂掉只说明「这次没领到」，与账号能不能
推理毫无关系。若把它记进账号池的错误计数，一次活动接口抖动就能把整池账号冷却 5 分钟，
这是实测踩过的坑（Go 版 `pool.Checkin` 的注释明确写了这一点）。
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional

import requests

from .const import (
    BENEFIT_HOST,
    CREDIT_CHANNEL,
    EP_BENEFIT_BALANCE,
    EP_BENEFIT_CLAIM,
    EP_BENEFIT_CONFIG,
    EP_CREDIT_CLAIM,
    EP_CREDIT_CONFIRM,
    EP_CREDIT_DELIVERY,
    SNAP_HOST,
)
from .signing import SignCredential, sign_request

# 只处理这两个活动：1=每日签到，4=新用户注册奖励
DAILY_CAMPAIGN_ID = 1
NEWCOMER_CAMPAIGN_ID = 4
SUPPORTED_CAMPAIGN_IDS = (DAILY_CAMPAIGN_ID, NEWCOMER_CAMPAIGN_ID)

# 积分单位；非 CREDIT 的活动（例如实物/优惠券）不在这里领取
CREDIT_UNIT = "CREDIT"

# 活动接口固定请求头（抓包确认）
CREDIT_HEADERS = {
    "Content-Type": "application/json",
    "Agent-Type": "PromptCenter",
    "X-Language": "zh-cn",
}


class CreditApiError(RuntimeError):
    """活动接口返回业务错误（envelope.code != 0）或 HTTP 错误。"""

    def __init__(self, message: str, code: int = 0, status: int = 0, path: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.path = path


def parse_rfc3339_nano(value: str) -> Optional[datetime.datetime]:
    """解析 RFC3339Nano（Go 的时间格式，小数秒最多 9 位）。

    Python 的 fromisoformat 只吃到 6 位小数秒，多余部分会直接报错，所以先截断。
    """
    if not value:
        return None
    text = str(value).strip()
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
class CampaignResult:
    """单个活动的领取结果。"""

    campaign_id: int = 0
    title: str = ""
    ok: bool = True
    status: str = ""
    credits: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "title": self.title,
            "ok": self.ok,
            "status": self.status,
            "credits": self.credits,
            "message": self.message,
        }


@dataclass
class CheckinResult:
    """一次签到的总结果。

    注意 `credits` 是**本次领取量**，不是账号余额 —— 面板不要把它当余额展示
    （Go 版就因为这点在 README 里专门澄清过）。
    """

    ok: bool = True
    credits: float = 0.0
    message: str = ""
    campaigns: list = field(default_factory=list)
    benefit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "credits": self.credits,
            "message": self.message,
            "campaigns": [c.to_dict() for c in self.campaigns],
            "benefit": self.benefit,
        }


class CreditActivity:
    """积分活动客户端（单个账号）。

    `transport` 可注入：单测里替换成假的请求函数，就能在无网络环境下完整覆盖状态机。
    """

    def __init__(
        self,
        cred: SignCredential,
        base_url: str = SNAP_HOST,
        timeout: int = 30,
        transport=None,
    ):
        self.cred = cred
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._http_request

    # --- 传输层 ---

    def _http_request(self, method: str, url: str, body: bytes) -> requests.Response:
        return sign_request(method, url, body, self.cred, CREDIT_HEADERS, timeout=self.timeout)

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """发一次活动请求，返回 envelope 里的 data。

        响应信封 `{code, message, data}`：`code != 0` 视为业务错误。注意 code 字段
        **必须存在** —— 缺字段说明拿到的不是活动接口的响应（例如被网关拦了），
        这时候当成成功会静默丢数据，所以显式报错。
        """
        import json as _json

        raw = b""
        if body is not None:
            raw = _json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = self.base_url + path
        response = self._transport(method, url, raw)
        status = getattr(response, "status_code", 200)
        text = getattr(response, "text", "") or ""
        if status >= 400:
            raise CreditApiError(
                f"活动接口 HTTP {status}: {text[:300]}", status=status, path=path
            )
        try:
            envelope = _json.loads(text) if text else {}
        except ValueError as error:
            raise CreditApiError(f"活动接口响应不是 JSON: {error}", path=path) from error
        if not isinstance(envelope, dict):
            raise CreditApiError("活动接口响应格式异常", path=path)
        if "code" not in envelope:
            raise CreditApiError("活动接口响应缺少业务码 code", path=path)
        code = envelope.get("code")
        if code not in (0, "0"):
            raise CreditApiError(
                f"活动业务错误 code={code} message={envelope.get('message')}",
                code=int(code) if str(code).lstrip("-").isdigit() else 0,
                path=path,
            )
        data = envelope.get("data")
        return data if isinstance(data, dict) else {}

    # --- 接口 ---

    def fetch_campaigns(self) -> list:
        """查询活动列表（只读）。"""
        data = self._request("GET", f"{EP_CREDIT_DELIVERY}?channel={CREDIT_CHANNEL}")
        items = data.get("items")
        if items is None:
            raise CreditApiError("活动接口响应缺少 data.items")
        return [item for item in items if isinstance(item, dict)]

    def claim(self, campaign_id: int, now: Optional[datetime.datetime] = None) -> None:
        """领取。idempotentKey 用毫秒时间戳保证同一毫秒内的重复调用被上游判重。"""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        key = f"claim_{campaign_id}_{int(now.timestamp() * 1000)}"
        self._request(
            "POST",
            EP_CREDIT_CLAIM,
            {"campaignId": campaign_id, "channel": CREDIT_CHANNEL, "idempotentKey": key},
        )

    def confirm(self, campaign_id: int) -> None:
        """确认到账。领取后必须确认，否则上游认为流程未完成。"""
        self._request("POST", EP_CREDIT_CONFIRM, {"campaignId": campaign_id})

    # --- 状态机 ---

    def claim_campaign(self, campaign: dict, now: Optional[datetime.datetime] = None) -> CampaignResult:
        """按状态机处理单个活动。"""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        campaign_id = int(campaign.get("campaignId") or 0)
        title = str(campaign.get("title") or "").strip() or f"积分活动 {campaign_id}"
        result = CampaignResult(
            campaign_id=campaign_id,
            title=title,
            status=str(campaign.get("status") or "").strip(),
            ok=True,
        )

        extra = campaign.get("extra") or {}
        end = parse_rfc3339_nano(extra.get("endTime") or "")
        if end is not None and now >= end:
            result.message = "活动已结束"
            return result
        start = parse_rfc3339_nano(extra.get("startTime") or "")
        if start is not None and now < start:
            result.message = "活动尚未开始"
            return result

        claimable = bool(campaign.get("claimable"))
        pending = _as_float(campaign.get("pendingTotalAmount"))
        amount = _as_float(campaign.get("benefitAmount"))

        if result.status in ("CONFIRMED", "CONSUMED"):
            result.message = "已领取"
            return result
        if result.status == "CLAIMED":
            # 已到账但尚未确认；不得重复领取
            pass
        elif result.status == "ELIGIBLE":
            if not claimable:
                result.ok = False
                result.message = "暂不可领取，稍后重试"
                return result
            try:
                self.claim(campaign_id, now)
            except (CreditApiError, requests.RequestException) as error:
                result.ok = False
                result.message = str(error)
                return result
            result.status = "CLAIMED"
            result.credits = pending if pending > 0 else amount
        elif result.status == "":
            result.message = "未获得活动资格"
            return result
        else:
            result.ok = False
            result.message = f"未知活动状态: {result.status}"
            return result

        try:
            self.confirm(campaign_id)
        except (CreditApiError, requests.RequestException) as error:
            result.ok = False
            result.message = f"已领取，确认失败，稍后重试: {error}"
            return result
        result.status = "CONFIRMED"
        result.message = "已补确认"
        if result.credits > 0:
            result.message = f"领取成功（{_fmt_amount(result.credits)} 积分）"
        return result

    def checkin(self) -> CheckinResult:
        """执行一次完整签到（每日 + 新手）。"""
        campaigns = self.fetch_campaigns()
        out = CheckinResult(ok=True, campaigns=[])
        now = datetime.datetime.now(datetime.timezone.utc)
        for campaign in campaigns:
            campaign_id = int(campaign.get("campaignId") or 0)
            if campaign_id not in SUPPORTED_CAMPAIGN_IDS:
                continue
            unit = str(campaign.get("benefitUnit") or "").strip()
            if unit and unit != CREDIT_UNIT:
                continue
            result = self.claim_campaign(campaign, now)
            out.campaigns.append(result)
            out.ok = out.ok and result.ok
            out.credits += result.credits
        parts = [f"{item.title}: {item.message}" for item in out.campaigns]
        out.message = "; ".join(parts) if parts else "无可领取的签到或新手活动"
        return out


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_amount(value: float) -> str:
    """积分数量格式化：整数不带小数点（对齐 Go 的 %g 观感）。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# ---------------------------------------------------------------------------
# opengw 免费额度签到（基线保留）
# ---------------------------------------------------------------------------

class BenefitClient:
    """opengw 免费额度网关客户端（单个账号）。

    基线用永久 AK/SK 直接访问；OAuth 凭证下同样带上 security_token 即可。
    """

    def __init__(
        self,
        cred: SignCredential,
        base_url: str = BENEFIT_HOST,
        timeout: int = 30,
        transport=None,
    ):
        self.cred = cred
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._http_request

    def _http_request(self, method: str, url: str, body: bytes) -> requests.Response:
        return sign_request(
            method,
            url,
            body,
            self.cred,
            {"Content-Type": "application/json", "X-Language": "zh-cn"},
            timeout=self.timeout,
        )

    def _unwrap(self, method: str, path: str, body: bytes = b"") -> dict:
        """opengw 的响应约定：error_code 为 0000（或缺失/0）表示成功。"""
        url = self.base_url + path
        response = self._transport(method, url, body)
        status = getattr(response, "status_code", 200)
        text = getattr(response, "text", "") or ""
        if status != 200:
            raise CreditApiError(f"HTTP {status}: {text[:300]}", status=status, path=path)
        try:
            import json as _json

            data = _json.loads(text)
        except ValueError as error:
            raise CreditApiError(f"响应不是 JSON: {error}", path=path) from error
        if not isinstance(data, dict):
            raise CreditApiError("响应格式异常", path=path)
        error_code = data.get("error_code")
        if error_code not in (None, "0000", 0, "0"):
            raise CreditApiError(
                f"业务错误 {error_code}: {data.get('error_msg')}", path=path
            )
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    def status(self) -> dict:
        """签到状态（create_time 即最近一次签到时间，毫秒时间戳）。"""
        return self._unwrap("GET", EP_BENEFIT_CLAIM)

    def claim(self) -> dict:
        """领取当日免费额度（空 body）。"""
        return self._unwrap("POST", EP_BENEFIT_CLAIM)

    def balance(self) -> dict:
        """免费额度余额。"""
        return self._unwrap("GET", EP_BENEFIT_BALANCE)

    def models(self) -> list:
        """免费模型列表。"""
        return self._unwrap("GET", EP_BENEFIT_CONFIG).get("models") or []


# ---------------------------------------------------------------------------
# 组合入口
# ---------------------------------------------------------------------------

def fetch_quota(
    cred: SignCredential,
    *,
    timeout: int = 30,
    client: Optional[BenefitClient] = None,
) -> dict:
    """查询账号的**可用额度**（opengw 免费额度通道）。

    **为什么只有这一个来源**：`/v1/ops/delivery` 只回活动列表（campaigns），
    里面**没有任何余额字段** —— `pendingTotalAmount` 是「待领取量」不是余额。
    逐个试过 `/v1/ops/balance`、`/v1/ops/credit`、`/v1/ops/usage`、
    `/v1/user/balance`、`/v1/account/balance`、`/v5/caller-identity` 等，
    全部 404（`APIG.0101 API does not exist`）。

    真正能查到「还剩多少」的只有 opengw 的 `/api/v1/user/tokens/balance`：

        {"total_balance": 9995562, "total_quota": 10000000, "used_amount": 4438,
         "daily_token_limit": 10000000, "daily_tokens_used": 4438, "expire_time": 0}

    返回归一化字典（保留上游字段名，前端直接展示），额外补 `fetched_at` 便于
    判断数据新旧。失败时抛 CreditApiError，由调用方决定是否忽略 —— 额度查询
    失败**绝不能**影响账号健康状态。
    """
    info = (client or BenefitClient(cred, timeout=timeout)).balance()
    quota = {
        "total_balance": _as_float(info.get("total_balance")),
        "total_quota": _as_float(info.get("total_quota")),
        "used_amount": _as_float(info.get("used_amount")),
        "daily_token_limit": _as_float(info.get("daily_token_limit")),
        "daily_tokens_used": _as_float(info.get("daily_tokens_used")),
        "monthly_token_limit": _as_float(info.get("monthly_token_limit")),
        "monthly_tokens_used": _as_float(info.get("monthly_tokens_used")),
        "expire_time": info.get("expire_time") or 0,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if quota["total_quota"] > 0:
        quota["percent_left"] = round(
            quota["total_balance"] / quota["total_quota"] * 100, 2
        )
    else:
        quota["percent_left"] = None
    return quota


def checkin_account(
    cred: SignCredential,
    *,
    credit_client: Optional[CreditActivity] = None,
    benefit_client: Optional[BenefitClient] = None,
    timeout: int = 30,
) -> CheckinResult:
    """对一个账号执行完整签到：积分活动 + opengw 免费额度。

    两套接口互相独立：活动接口挂了不该影响 opengw 领取，反之亦然。所以这里分别
    try/except，把失败信息写进 message 而不是抛出 —— 调用方（调度器/WebUI）只需要
    一个「成没成、为什么」的结论。
    """
    credit = credit_client or CreditActivity(cred, timeout=timeout)
    benefit = benefit_client or BenefitClient(cred, timeout=timeout)

    out = CheckinResult(ok=True, campaigns=[], benefit={})
    messages = []

    try:
        credit_result = credit.checkin()
        out.campaigns = credit_result.campaigns
        out.credits = credit_result.credits
        out.ok = out.ok and credit_result.ok
        messages.append(credit_result.message)
    except (CreditApiError, requests.RequestException) as error:
        out.ok = False
        messages.append(f"积分活动失败: {error}")

    try:
        info = benefit.claim()
        create_time = info.get("create_time")
        out.benefit = {
            "ok": True,
            "message": f"免费额度已领取（{_fmt_time(create_time)}）",
            "create_time": create_time,
        }
    except (CreditApiError, requests.RequestException) as error:
        out.ok = False
        out.benefit = {"ok": False, "message": f"免费额度领取失败: {error}"}
        messages.append(f"免费额度失败: {error}")

    out.message = " | ".join(part for part in messages if part) or "签到完成"
    return out


def format_time_ms(ms) -> str:
    """毫秒时间戳 → 本地时间字符串（baseline benefit.py 的展示逻辑）。"""
    if not ms:
        return "从未签到"
    try:
        local = datetime.datetime.fromtimestamp(float(ms) / 1000)
    except (TypeError, ValueError, OSError):
        return "时间未知"
    return local.strftime("%Y-%m-%d %H:%M:%S")


_fmt_time = format_time_ms
