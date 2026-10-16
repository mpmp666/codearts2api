# -*- coding: utf-8 -*-
"""上游聊天客户端：请求构造、SSE 聚合、流式转发、错误分类。

**双通道签名（基线行为，必须逐字节保留）**：
  * agent 通道（老套餐模型）→ `signing.chat_headers(benefit=False)`
  * benefit 通道（免费额度模型）→ 额外签 `maas_type` + `model-id` + `model-name`
两条通道严格区分：老套餐模型带 benefit 头会报 "unsupported model"，免费模型不带会报
"The model is not registered"。判定依据是**发起请求的账号自己的模型目录**
（`registry.is_benefit_model(uid, model)`），不能全局共享 —— 池里各账号套餐可能不同。

**SSE 处理保留基线的全部细节**：
  * 非流式：上游即使返回 `text/event-stream` 也聚合成单个 JSON（CodeArts 在排队/重试时
    会这么干，直接转发会让客户端把 `data:` 当 JSON 解析）
  * 流式：agent 流保持即时转发（不改变老客户端状态机）；benefit 流补齐缺失的
    `finish_reason`，并把 `content: null` 改成 `""`（DeepSeek benefit 流会发
    content:null + reasoning_content 有值，部分 Agent Runtime 会把 null 误判为无响应）

**错误分类决定处罚方式**（这是最容易把整池打挂的地方）：
  * `InferHub.001001005` 参数错误 → 400，**不轮换、不冷却账号**（客户端请求形状问题）
  * `not registered` / `benefit not found` → 模型对该账号不可用，**不记账号错误**
  * 401 → 鉴权失效（终态，禁用）
  * 429 / 并发会话 / TPM → 瞬时限流，原地等待重试，**不冷却**
  * 3012 / 405 unusual activity → 风控，指数退避
  * 5xx → 重试耗尽后冷却
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

import requests

from .const import EP_CHAT_V2, SNAP_HOST
from .models import AVAIL_UNKNOWN, AVAIL_UNUSABLE, ModelRegistry, upstream_unavailable_reason
from .signing import chat_headers, json_body

# 流式请求的读超时：长文本生成可能很久，基线用的是 600s
STREAM_TIMEOUT = 600
# 非流式聚合请求的超时
NONSTREAM_TIMEOUT = 600


class UpstreamError(RuntimeError):
    """上游错误基类。"""

    kind = "upstream"

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def message(self) -> str:
        return str(self)


class ParamInvalidError(UpstreamError):
    """上游拒绝请求参数（InferHub.001001005）—— 客户端问题，不处罚账号。"""

    kind = "param_invalid"


class ModelUnavailableError(UpstreamError):
    """模型对该账号不可用（未注册 / 福利未领取）—— 账号能力问题，不处罚账号。"""

    kind = "model_unavailable"

    def __init__(self, message: str, reason: str = "", **kwargs):
        super().__init__(message, **kwargs)
        self.reason = reason


class AuthFailedError(UpstreamError):
    """401 鉴权失效（终态）。"""

    kind = "auth"


class QueueLimitError(UpstreamError):
    """并发会话 / TPM 限流（瞬时，原地等待重试）。"""

    kind = "queue"


class RiskControlError(UpstreamError):
    """风控（unusual activity）—— 指数退避。"""

    kind = "risk"


class UpstreamServerError(UpstreamError):
    """5xx（重试耗尽后冷却）。"""

    kind = "server"


def normalize_finish_reason(value):
    """把 CodeArts 的结束原因转换为 OpenAI 客户端能识别的值。"""
    if value in (None, "other"):
        return "stop"
    return value


def is_param_invalid(text: str) -> bool:
    """是否是「客户端请求形状问题」（而不是账号故障）。

    两类：
      1. 已知的参数错误码 `InferHub.001001005` / `request param is invalid`。
      2. **上游把业务码与 HTTP 状态拼在消息里**，形如
         `InferHub.001001044.400 Message role cannot empty`。这类 4xx 一律是
         客户端发错了东西，绝不能算账号故障 —— 否则一个客户端发错字段就能把
         整池账号冷却甚至禁用（实测：DSH 发 `role:"developer"` 就把两个号
         全打进了冷却，返回 503 `all accounts unavailable`）。
    """
    low = (text or "").lower()
    if "inferhub.001001005" in low or "request param is invalid" in low:
        return True
    if is_concurrent_limit(text):
        # 400 的并发限制是**瞬时**状态，要留给 QueueLimitError 原地重试，
        # 不能被这里吞成「参数错误」。
        return False
    embedded = embedded_http_status(text)
    return embedded is not None and embedded in CLIENT_ERROR_STATUSES


# 客户端错误状态码：请求本身有问题，与账号健康无关。
#
# ⚠️ **刻意不含 405**：在这个上游 405 是**风控**（unusual activity），
# `is_risk_control()` 就是按 `status == 405` 判定的，要走进指数退避。
# 若把 405 也算客户端错误，`is_param_invalid()` 在 classify_error 里排在风控
# 之前，会把风控吞掉 —— 风控信号丢失比误冷却更危险（会持续施压被封的账号）。
CLIENT_ERROR_STATUSES = frozenset({400, 404, 409, 413, 414, 415, 422})

_EMBEDDED_STATUS_RE = re.compile(r"inferhub\.[0-9a-z]+\.(\d{3})", re.IGNORECASE)


def embedded_http_status(text: str) -> Optional[int]:
    """从上游消息里取出内嵌的 HTTP 状态（`InferHub.<code>.<status> ...`）。"""
    match = _EMBEDDED_STATUS_RE.search(text or "")
    return int(match.group(1)) if match else None


def is_concurrent_limit(text: str) -> bool:
    low = (text or "").lower()
    return "tm.00001041" in low or "并发会话" in low


def is_queue_limit(status: int, text: str) -> bool:
    low = (text or "").lower()
    return (
        status == 429
        or (status == 400 and is_concurrent_limit(text))
        or "inferhub.modelarts.81111.429" in low
        or "tpm limit" in low
    )


def is_risk_control(status: int, text: str) -> bool:
    """风控判定。

    3012 / 405 是华为云侧的异常活动标记（unusual activity）：它通常来自短时间高频调用，
    等待越久越安全，所以走指数退避而不是固定冷却。
    """
    low = (text or "").lower()
    if "unusual activity" in low or "3012" in (text or ""):
        return True
    return status == 405


def classify_error(status: int, text: str) -> UpstreamError:
    """把上游响应归类成具体异常。

    **判定顺序有讲究**：账号级信号（模型不可用 / 风控 / 鉴权）优先于请求形状信号
    （参数错误）。反过来的话，一个消息里内嵌了 `.400` 的 405 风控响应会被判成
    「客户端参数错误」—— 风控信号丢失意味着会继续对已被风控的账号施压，
    比误冷却一个健康账号危险得多。
    """
    reason = upstream_unavailable_reason(text)
    if reason:
        return ModelUnavailableError(text, reason, status=status)
    if is_risk_control(status, text):
        return RiskControlError(text, status=status)
    if is_param_invalid(text):
        return ParamInvalidError(text, status=status)
    if status in (401, 403):
        return AuthFailedError(text, status=status)
    if is_queue_limit(status, text):
        return QueueLimitError(text, status=status)
    if status in CLIENT_ERROR_STATUSES:
        # 兜底：上游直接回 4xx（消息里没有内嵌状态码时）。放在 queue 之后，
        # 保证 429 / 400+并发限制 仍走「原地重试」而不是被判成参数错误。
        return ParamInvalidError(text, status=status)
    if status >= 500:
        return UpstreamServerError(text, status=status)
    return UpstreamError(text, status=status)


# ---------------------------------------------------------------------------
# SSE 解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def parse_sse_bytes(raw: bytes) -> dict:
    """把上游 SSE 聚合为一个 OpenAI chat.completion JSON（基线行为）。"""
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    result = {
        "id": None,
        "object": "chat.completion",
        "created": None,
        "model": None,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
    }
    usage = None
    last_finish_reason = None

    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].lstrip()
        if payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        # CodeArts 可能以 HTTP 200 返回业务错误，需要保留错误信息
        if chunk.get("error_code") or chunk.get("error_msg"):
            return {
                "error": {
                    "message": chunk.get("error_msg") or "CodeArts upstream error",
                    "type": "upstream_error",
                    "code": chunk.get("error_code"),
                }
            }

        for key in ("id", "created", "model"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason is not None:
                last_finish_reason = normalize_finish_reason(reason)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                current = tool_calls.setdefault(
                    index,
                    {
                        "id": call.get("id"),
                        "type": call.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call.get("id"):
                    current["id"] = call["id"]
                if call.get("type"):
                    current["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    current["function"]["name"] += function["name"]
                if function.get("arguments"):
                    current["function"]["arguments"] += function["arguments"]

    message = result["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    result["choices"][0]["finish_reason"] = last_finish_reason or (
        "tool_calls" if tool_calls else "stop"
    )
    if usage:
        result["usage"] = usage
    return result


def parse_upstream_json(response) -> dict:
    """兼容上游普通 JSON 和错误地返回 SSE 的情况（基线行为）。"""
    raw = response.content
    try:
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, dict) and result.get("choices"):
            for choice in result["choices"]:
                choice["finish_reason"] = normalize_finish_reason(choice.get("finish_reason"))
        return result
    except (UnicodeDecodeError, json.JSONDecodeError):
        return parse_sse_bytes(raw)


def embedded_error(result: dict) -> Optional[UpstreamError]:
    """从聚合结果里提取上游内嵌错误（HTTP 200 + error_code）。"""
    if not isinstance(result, dict):
        return None
    error = result.get("error")
    if not error:
        return None
    text = f"{error.get('code')} {error.get('message')}"
    return classify_error(200, text)


def estimate_usage(messages, content: str, reasoning: str) -> dict:
    """上游没给 usage 时按字符数估算（不是精确 token 计数，只保证字段齐全）。"""
    prompt_tokens = 0
    for message in messages or []:
        raw = message.get("content") if isinstance(message, dict) else None
        if isinstance(raw, str):
            prompt_tokens += len(raw) // 4 + 1
        elif raw is not None:
            prompt_tokens += len(json.dumps(raw, ensure_ascii=False)) // 4 + 1
    completion_tokens = (len(content or "") + len(reasoning or "")) // 4 + 1
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def stream_frames(
    response,
    model: str,
    is_benefit: bool,
    include_usage: bool = False,
    messages=None,
):
    """把上游响应转发为 OpenAI SSE（基线行为 + 流式 usage）。

    `include_usage=True` 时在 `[DONE]` 之前额外发一次 `choices: []` 的 usage chunk；
    未启用则完全不发（与基线一致）。
    """
    content_type = (response.headers.get("Content-Type") or "").lower()
    done_emitted = False       # 是否已经发过 [DONE]
    usage_emitted = False      # 是否已经发过 usage 帧（避免重复）
    saw_finish_reason = False
    usage = None
    text_parts = []
    reasoning_parts = []

    def usage_frame() -> bytes:
        final_usage = usage or estimate_usage(
            messages, "".join(text_parts), "".join(reasoning_parts)
        )
        return b"data: " + json.dumps(
            {
                "id": "proxy-usage",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": final_usage,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n\n"

    def finish_frame() -> bytes:
        return b"data: " + json.dumps(
            {
                "id": "proxy-finish",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n\n"

    # 上游偶尔会在请求 stream=true 时返回普通 JSON，包装成单个 SSE 帧
    if "text/event-stream" not in content_type:
        result = parse_upstream_json(response)
        for choice in result.get("choices") or []:
            if choice.get("finish_reason") in (None, "other"):
                choice["finish_reason"] = "stop"
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        yield b"data: " + payload + b"\n\n"
        if include_usage:
            usage = result.get("usage") or None
            yield usage_frame()
            usage_emitted = True
        yield b"data: [DONE]\n\n"
        return

    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        if not raw_line.startswith(b"data:"):
            # SSE 注释行（心跳）原样转发
            if raw_line.startswith(b":"):
                yield raw_line + b"\n\n"
            continue

        payload = raw_line[5:].strip()
        if payload == b"[DONE]":
            # benefit 流需要先确认是否缺少 finish_reason；agent 流保持原来的即时转发
            # 行为，避免改变 agent 客户端状态机。
            if is_benefit:
                continue  # benefit 的 [DONE] 统一延迟到收尾阶段补发
            if include_usage and not usage_emitted:
                yield usage_frame()
                usage_emitted = True
            if not done_emitted:
                yield b"data: [DONE]\n\n"
                done_emitted = True
            continue
        try:
            chunk = json.loads(payload.decode("utf-8"))
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                reason = choice.get("finish_reason")
                if reason is not None:
                    saw_finish_reason = True
                    if reason == "other":
                        choice["finish_reason"] = "stop"
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    # DeepSeek benefit 流会发送 content:null、reasoning_content 有值。
                    # 部分 Agent Runtime 只接受字符串 content，会将 null chunk 误判为
                    # 无响应；OpenAI 兼容格式中空字符串表达同样语义。
                    if delta.get("content") is None:
                        delta["content"] = ""
                    if isinstance(delta.get("content"), str):
                        text_parts.append(delta["content"])
                    if isinstance(delta.get("reasoning_content"), str):
                        reasoning_parts.append(delta["reasoning_content"])
            payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            yield b"data: " + payload + b"\n\n"
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

    # 只对免费 benefit 模型补齐完全缺失的 finish_reason；不重发任何上游 chunk。
    if is_benefit and not saw_finish_reason:
        yield finish_frame()
    if include_usage and not usage_emitted:
        # 上游断流没发 [DONE]、或 benefit 流的 [DONE] 被延迟到这里，都要把 usage 补上
        yield usage_frame()
        usage_emitted = True
    if is_benefit:
        # 上游 [DONE] 被延迟到这里，确保补块在 [DONE] 之前
        yield b"data: [DONE]\n\n"
    elif not done_emitted:
        # agent 分支保持原有行为：上游异常断流、没有发送 [DONE] 时代理仍补一个
        yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# tool_choice
# ---------------------------------------------------------------------------

def parse_tool_choice(raw) -> tuple:
    """归一化 tool_choice，返回 `(mode, function_name)`。

    mode ∈ {"auto", "none", "required", "function"}；未提供视为 auto。
    """
    if raw is None or raw == "" or raw == "null":
        return "auto", ""
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in ("none", "required"):
            return lowered, ""
        return "auto", ""
    if isinstance(raw, dict):
        function = raw.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else ""
        if raw.get("type") == "function" and name:
            return "function", str(name)
    return "auto", ""


def apply_tool_choice(tools, choice: tuple) -> tuple:
    """按 tool_choice 调整 tools，并给出上游接受的字符串形式。

    上游（InferHub）只接受字符串形式的 tool_choice，对象形式会被判参数无效
    （`InferHub.001001005`，实测）。因此指定函数时：只向上游发送该函数的定义，
    并把 tool_choice 转成 `required` —— 只留一个可选函数 + required 等价于「必须调它」。
    """
    mode, function = choice
    if mode != "function":
        return tools, (mode if mode in ("none", "required") else "auto")

    selected = None
    for tool in tools or []:
        definition = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(definition, dict) or definition.get("name") != function:
            continue
        if selected is not None:
            raise ValueError(f"tool_choice 指定的函数 {function!r} 在 tools 里重复定义")
        selected = tool
    if selected is None:
        raise ValueError(f"tool_choice 指定的函数 {function!r} 不在 tools 列表里")
    return [selected], "required"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

# 上游对 max_tokens 有**硬上限 65536**，超过它不是报错，而是返回 HTTP 200 +
# **内容全空**（实测二分：65536 正常，66048 起 content 与 reasoning_content 全为空；
# GLM-5.2 与 deepseek 系列阈值一致）。
#
# 上游在 `/v1/models` 里自报的 `max_tokens`（384000 / 393216 / 131072 …）
# **全部超过这个上限**，照抄它去声明模型的输出上限，就会让客户端每次都收到一个
# 「成功但什么都没有」的响应 —— DSH 上 `deepseek-v4.1-flash` 返回空正文就是这个原因。
# 所以这里按**实测可用上限**夹住，宁可少给输出也不能给空响应。
MAX_OUTPUT_TOKENS = 65536


def configured_max_output_tokens() -> int:
    """允许用 `CA2A_MAX_OUTPUT_TOKENS` 覆盖实测上限（上游改了就无需改代码）。

    设 0 或负数表示**关闭夹取**（想原样透传时用，但会重现空响应）。
    """
    raw = (os.getenv("CA2A_MAX_OUTPUT_TOKENS") or "").strip()
    if not raw:
        return MAX_OUTPUT_TOKENS
    try:
        return int(raw)
    except ValueError:
        return MAX_OUTPUT_TOKENS


def clamp_max_tokens(body: dict, limit: Optional[int] = None) -> dict:
    """把超出上游硬上限的 max_tokens 压回可用范围。

    两种字段名都处理：OpenAI 新版用 `max_completion_tokens`，老版与多数
    兼容客户端用 `max_tokens`。只夹不报错 —— 客户端要 200K 输出而上游只给 64K，
    返回真实内容远好过返回一个空的成功响应。
    """
    limit = configured_max_output_tokens() if limit is None else limit
    if limit <= 0:
        return body
    for field in ("max_tokens", "max_completion_tokens"):
        value = body.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > limit:
            body[field] = limit
    return body


def normalize_message_roles(messages):
    """把客户端可能使用、而**上游不认识**的消息角色归一化。

    ⚠️ `role: "developer"` 是 OpenAI 在 o1 之后引入的「system 的替代角色」，
    DSH 0.1.7 起用它承载系统提示词（实测那条消息约 8 KB，就是整份系统提示词）。
    华为上游**不认识这个角色**，收到就整轮失败：

        {"messages":[{"role":"developer",...},{"role":"user",...}]}
        → InferHub.001001044.400 Message role cannot empty

    实测隔离过：同一请求**只把 developer 换成 system** 就是 200；其余 DSH 会发的
    字段（`stream_options` / `store` / `max_completion_tokens`）和 36 个 tools
    都能正常通过 —— **只有这一个角色会触发失败**。

    `developer` 的语义就是 system，故**改名而不是丢弃**：丢了会静默丢掉整份
    系统提示词，模型行为会莫名其妙地变差，且完全没有报错。

    返回新列表 + 新字典，不原地改调用方的对象（上游 body 与用量估算共用同一份
    messages，原地改会带来难以排查的副作用）。
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "developer":
            copied = dict(message)
            copied["role"] = "system"
            out.append(copied)
        else:
            out.append(message)
    return out


def build_body(
    request_json: dict,
    *,
    model: str,
    canonical_model: str,
    chat_id: str = "",
    tools=None,
    tool_choice: str = "auto",
) -> bytes:
    """构造发给上游的请求体。

    基线是「原样转发客户端 body，只替换 model」—— 这里保持同样的策略（客户端可自由
    使用 temperature / top_p / max_tokens / response_format 等上游支持的任何字段），
    只做四件事：
      1. model 归一到上游注册的精确大小写
      2. 补 chat_id（多轮上下文分组 / prompt cache 亲和）
      3. 按 tool_choice 收窄 tools
      4. 强制 stream=true（上游 v2 端点始终返回 SSE，非流式由代理聚合）
    """
    body = dict(request_json)
    body["model"] = canonical_model
    # DSH 用 role=developer 装系统提示词，上游不认 —— 必须改名成 system
    body["messages"] = normalize_message_roles(body.get("messages"))
    # 上游 max_tokens 硬上限 65536，超过会返回「成功但空内容」
    clamp_max_tokens(body)
    if chat_id:
        body["chat_id"] = chat_id
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    elif "tool_choice" in body:
        # 没有 tools 时不要给上游发 tool_choice，否则可能被判参数无效
        body.pop("tool_choice", None)
    # 上游始终以 SSE 返回；非流式由代理侧聚合，这样排队/重试行为一致
    body["stream"] = True
    return json_body(body)


class UpstreamClient:
    """CodeArts 聊天/探测客户端。"""

    def __init__(self, cfg, registry: ModelRegistry):
        self.cfg = cfg
        self.registry = registry
        self.chat_url = SNAP_HOST + EP_CHAT_V2

    # --- 发送 ---

    def send(
        self,
        cred,
        body: bytes,
        *,
        benefit: bool,
        model_id: str,
        timeout: int = STREAM_TIMEOUT,
    ) -> requests.Response:
        """发一次聊天请求（stream=True，返回未读完的 Response）。"""
        headers = chat_headers(cred, body, benefit=benefit, model_id=model_id)
        response = requests.post(
            self.chat_url, data=body, headers=headers, stream=True, timeout=(30, timeout)
        )
        if response.status_code != 200:
            text = _safe_text(response)
            response.close()
            raise classify_error(response.status_code, text)
        return response

    def probe(self, account, model: str, timeout: int = 45):
        """用最小请求探测模型可用性。

        返回 `(usable, reason)` 或 None（无法判定 —— 例如撞上并发上限，这是暂时状态，
        不代表模型不可用，绝不能记成不可用）。
        """
        canonical = self.registry.canonical_model(model)
        body = json_body(
            {
                "model": canonical,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        benefit = self.registry.is_benefit_model(account.uid, model)
        try:
            response = self.send(
                account.cred.sign_credential(),
                body,
                benefit=benefit,
                model_id=canonical,
                timeout=timeout,
            )
        except ModelUnavailableError as error:
            return False, error.reason
        except QueueLimitError:
            return None
        except (RiskControlError, ParamInvalidError, AuthFailedError, UpstreamServerError, UpstreamError):
            return None
        except requests.RequestException:
            return None

        # 200 也要看内容：上游会把错误塞在 200 的 SSE 里
        head = []
        try:
            for index, line in enumerate(response.iter_lines(decode_unicode=False)):
                head.append(line)
                if index >= 5:
                    break
        except requests.RequestException:
            return None
        finally:
            response.close()
        text = b"\n".join(line for line in head if line).decode("utf-8", "replace")
        if not text:
            return None
        reason = upstream_unavailable_reason(text)
        if reason:
            return False, reason
        if "error_code" in text or "error_msg" in text:
            # 其他业务错误：不能判定为「模型不可用」，交给真实流量去学
            return None
        return True, ""


def _safe_text(response) -> str:
    try:
        return (response.text or "")[:2000]
    except Exception:
        return ""


def availability_state(registry: ModelRegistry, uid: str, model: str) -> int:
    """便捷读取可用性结论（server 层用）。"""
    state, _ = registry.availability.get(uid, model)
    return state


__all__ = [
    "AVAIL_UNKNOWN",
    "AVAIL_UNUSABLE",
    "AuthFailedError",
    "ModelUnavailableError",
    "ParamInvalidError",
    "QueueLimitError",
    "RiskControlError",
    "UpstreamClient",
    "UpstreamError",
    "UpstreamServerError",
    "apply_tool_choice",
    "build_body",
    "classify_error",
    "embedded_error",
    "estimate_usage",
    "normalize_finish_reason",
    "parse_sse_bytes",
    "parse_tool_choice",
    "parse_upstream_json",
    "stream_frames",
]
