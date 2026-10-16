# -*- coding: utf-8 -*-
"""模型发现 + 可用性探测 + 限时福利领取。

模型来自三路（对齐 Go 版 `internal/upstream/models.go` 与基线抓包结果）：

1. **AgentCenter**：`useragents` 找 CodeAgent（alias 智能体 + show_in_ide）→ `detail`
   的 `gpts.models`。这是基线唯一的来源，走**基础签名**（agent 通道）。
2. **内置**：`GET /v1/model/builtin`（`Agent-Type: PromptCenter`）。
3. **限时福利**：`GET {opengw}/api/v1/gateway/config`，整体标记 `benefit=True`。

**目录按账号隔离**：福利是按账号授予的，池里 A 账号有、B 账号没有时不允许互相污染，
所以 `is_benefit_model(uid, model)` 必须带账号身份查询。

**为什么需要可用性探测**：上游的模型发现会列出当前账号根本没注册的模型
（实测返回 `InferHub.002002009.404`），福利模型未领取时返回 `InferHub.4004.200
benefit not found`。只按发现结果列模型，客户端点开就是一堆必然失败的条目。因此这里
用一条最小请求真实探测并缓存结论（**30 分钟**），`/v1/models` 只列真实可用的模型。

**能力问题 ≠ 账号故障**：`not registered` / `benefit not found` 记为「这个账号用不了
这个模型」，**不给健康账号记错误冷却** —— 否则调用一个未注册模型就能把整池账号冷却。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .atomic import write_json_atomic
from .const import (
    BENEFIT_MODELS,
    EP_AGENT_DETAIL,
    EP_AGENT_LIST,
    EP_BENEFIT_CLAIM,
    EP_BENEFIT_CONFIG,
    EP_MODEL_BUILTIN,
    FALLBACK_AGENT_MODELS,
    FALLBACK_MODELS,
    SNAP_HOST,
)
from .signing import SignCredential, sign_request

# 账号模型目录的有效期（超过则建议重新发现）
CATALOG_TTL_SECONDS = 3600.0
# 模型发现失败后的负缓存（避免每次 /v1/models 都空等超时）
FETCH_FAIL_COOLDOWN = 300.0
# 某一来源（内置/福利）连续失败后的退避窗口
SOURCE_FAIL_COOLDOWN = 300.0

# 可用性结论
AVAIL_UNKNOWN = 0
AVAIL_USABLE = 1
AVAIL_UNUSABLE = 2


@dataclass
class ModelInfo:
    """一个模型条目。"""

    id: str
    name: str = ""
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None
    benefit: bool = False
    desc: str = ""
    channel: str = "agent"
    supports_images: bool = False
    enable_queue: bool = False

    def to_cache_entry(self) -> dict:
        """落盘到 models-cache.json 的形态（**保持基线字段不变**）。"""
        entry = {
            "id": self.id,
            "object": "model",
            "owned_by": "codearts",
            "channel": self.channel,
            "name": self.name or self.id,
            "description": self.desc or "",
            "context_window": self.context_window,
            "max_tokens": self.max_tokens,
            "supports_images": self.supports_images,
        }
        if self.channel == "agent":
            entry["enable_queue"] = self.enable_queue
        return entry

    @classmethod
    def from_cache_entry(cls, entry: dict) -> "ModelInfo":
        channel = str(entry.get("channel") or "agent")
        return cls(
            id=str(entry.get("id") or ""),
            name=str(entry.get("name") or ""),
            context_window=entry.get("context_window"),
            max_tokens=entry.get("max_tokens"),
            benefit=channel == "benefit",
            desc=str(entry.get("description") or ""),
            channel=channel,
            supports_images=bool(entry.get("supports_images")),
            enable_queue=bool(entry.get("enable_queue")),
        )

    def to_openai_payload(self) -> dict:
        """OpenAI /v1/models 条目（含常见能力元数据，未知字段客户端会安全忽略）。"""
        context_window = self.context_window
        max_tokens = self.max_tokens
        payload = {
            "id": self.id,
            "object": "model",
            "created": 0,
            "owned_by": "codearts",
            "name": self.name or self.id,
            "description": self.desc or "",
            "context_window": context_window,
            "context_length": context_window,
            "max_tokens": max_tokens,
            "max_output_tokens": max_tokens,
            "supports_images": self.supports_images,
            "vision": self.supports_images,
            "supports_vision": self.supports_images,
            "supports_function_calling": True,
            "supports_tool_calling": True,
            "supports_reasoning": True,
            "input_modalities": ["text", "image"] if self.supports_images else ["text"],
            "output_modalities": ["text"],
            "supported_parameters": [
                "temperature",
                "top_p",
                "max_tokens",
                "stream",
                "tools",
                "tool_choice",
                "response_format",
            ],
        }
        if self.benefit:
            # 面板/客户端据此提示「这是需要领取的限时福利模型」
            payload["benefit"] = True
        return {key: value for key, value in payload.items() if value is not None}


def merge_models(*sets: list) -> list:
    """合并多路模型列表：按小写 ID 去重，福利标记取或，参数更全的优先。

    结果按小写 ID 排序，保证 /v1/models 输出稳定（缓存刷新前后顺序一致）。
    签名与 Go 版 `MergeModels(sets ...[]ModelInfo)` 一致（可变参数）。
    """
    merged: dict = {}
    for models in sets:
        for info in models or []:
            if not info.id:
                continue
            key = info.id.lower()
            current = merged.get(key)
            if current is None:
                merged[key] = info
                continue
            merged[key] = _richer(current, info)
    out = list(merged.values())
    out.sort(key=lambda item: (item.id.lower(), item.id))
    return out


def _richer(current: ModelInfo, other: ModelInfo) -> ModelInfo:
    """同一模型两条记录：福利标记取或（漏标就是漏一个 maas_type 头 → 404）。"""
    benefit = current.benefit or other.benefit
    keep, drop = current, other
    if _better(other, current):
        keep, drop = other, current
    keep.benefit = benefit
    keep.name = keep.name or drop.name
    keep.context_window = keep.context_window or drop.context_window
    keep.max_tokens = keep.max_tokens or drop.max_tokens
    keep.desc = keep.desc or drop.desc
    keep.channel = keep.channel if keep.channel == "benefit" else drop.channel
    return keep


def _better(a: ModelInfo, b: ModelInfo) -> bool:
    if bool(a.context_window) != bool(b.context_window):
        return bool(a.context_window)
    return a.id != a.id.lower() and b.id == b.id.lower()


class AvailabilityStore:
    """模型可用性结论（按「账号 + 模型」缓存 30 分钟）。"""

    def __init__(self, ttl_seconds: float = 1800.0):
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        self._by_key: dict = {}
        self._probing: set = set()
        self.last_sweep = 0.0
        self._last_traffic: dict = {}

    @staticmethod
    def key(uid: str, model: str) -> str:
        return f"{uid}|{(model or '').lower()}"

    def get(self, uid: str, model: str) -> tuple:
        with self._lock:
            entry = self._by_key.get(self.key(uid, model))
            if entry is None or (time.time() - entry[2]) > self.ttl:
                return AVAIL_UNKNOWN, ""
            return entry[0], entry[1]

    def mark(self, uid: str, model: str, state: int, reason: str = "") -> None:
        if not uid or not model:
            return
        with self._lock:
            self._by_key[self.key(uid, model)] = (state, reason, time.time())

    def mark_usable(self, uid: str, model: str) -> None:
        self.mark(uid, model, AVAIL_USABLE)

    def mark_unusable(self, uid: str, model: str, reason: str) -> None:
        self.mark(uid, model, AVAIL_UNUSABLE, reason)

    def note_traffic(self, uid: str) -> None:
        """记录账号最后一次真实请求时间（探测据此避让）。"""
        if not uid:
            return
        with self._lock:
            self._last_traffic[uid] = time.time()

    def traffic_idle_seconds(self, uid: str) -> float:
        with self._lock:
            last = self._last_traffic.get(uid)
        if last is None:
            return float("inf")
        return time.time() - last

    def begin_probe(self, uid: str, model: str) -> bool:
        """标记「正在探测」去重。返回 False 表示已有探测在跑。"""
        key = self.key(uid, model)
        with self._lock:
            if key in self._probing:
                return False
            self._probing.add(key)
            return True

    def end_probe(self, uid: str, model: str) -> None:
        with self._lock:
            self._probing.discard(self.key(uid, model))

    def all_keys(self) -> list:
        with self._lock:
            return list(self._by_key)

    def known_unusable(self, uid: str, model: str) -> bool:
        return self.get(uid, model)[0] == AVAIL_UNUSABLE


class ModelRegistry:
    """账号模型目录 + 大小写归一（进程级单例，由 server 持有）。"""

    def __init__(self, availability: Optional[AvailabilityStore] = None):
        self._lock = threading.RLock()
        self._catalogs: dict = {}
        self._known: dict = {}
        self.availability = availability or AvailabilityStore()
        for model_id in list(FALLBACK_AGENT_MODELS) + list(BENEFIT_MODELS):
            self._known.setdefault(model_id.lower(), model_id)

    # --- 目录 ---

    def set_account_models(self, uid: str, models: list, keep_benefit: bool = False) -> None:
        """整体替换某账号的模型目录（不是增补：套餐轮换后旧模型不得残留）。

        keep_benefit=True 表示本轮福利来源失败（内置来源成功）：此时保留上一轮确认过的
        福利条目，否则非种子的福利模型会瞬间丢掉 `maas_type` 标记，直到下次刷新成功为止，
        这期间它们在上游按未注册模型 404。
        """
        if not uid:
            return
        benefit = {}
        known = {}
        infos = list(models)
        with self._lock:
            previous = self._catalogs.get(uid) or {}
            last_benefit = dict(previous.get("last_benefit") or {})
            for info in infos:
                if not info.id:
                    continue
                key = info.id.lower()
                known[key] = True
                if info.benefit:
                    benefit[key] = True
                    last_benefit[key] = info
                self._known.setdefault(key, info.id)
            if keep_benefit:
                for key, info in last_benefit.items():
                    if key in known:
                        continue  # 内置来源明确登记了它 → 已转正，不保留标记
                    benefit[key] = True
                    known[key] = True
                    infos.append(info)
                    self._known.setdefault(key, info.id)
                infos.sort(key=lambda item: (item.id.lower(), item.id))
            self._catalogs[uid] = {
                "infos": infos,
                "benefit": benefit,
                "known": known,
                "last_benefit": last_benefit,
                "fetched": time.time(),
            }

    def account_models(self, uid: str) -> tuple:
        """返回 `(models, known)`；known=False 表示该账号目录还没发现过。"""
        with self._lock:
            catalog = self._catalogs.get(uid)
            if catalog is None:
                return [], False
            return list(catalog["infos"]), True

    def account_catalog_stale(self, uid: str) -> bool:
        with self._lock:
            catalog = self._catalogs.get(uid)
            if catalog is None:
                return True
            return (time.time() - catalog["fetched"]) >= CATALOG_TTL_SECONDS

    def is_benefit_model(self, uid: str, model: str) -> bool:
        """报告该账号下该模型是否走限时福利路由（大小写不敏感）。

        判定顺序：
          1. 该账号目录里标了福利 → True（发现结果优先于种子）
          2. 该账号目录里有但没标福利 → False（已以内置/agent-center 身份注册，
             套餐轮换后福利模型转正就靠这条摘掉 maas_type 头）
          3. 目录里没有这个模型（尚未发现/福利来源失败/未领取）→ 回退冷启动种子，
             少带一次头就是一次 InferHub.002002009.404，宁可多带
        """
        key = (model or "").strip().lower()
        if not key:
            return False
        with self._lock:
            catalog = self._catalogs.get(uid)
            if catalog:
                if catalog["benefit"].get(key):
                    return True
                if catalog["known"].get(key):
                    return False
            return key in {m.lower() for m in BENEFIT_MODELS}

    def canonical_model(self, model: str) -> str:
        """把用户写的模型 ID 归一为上游注册的精确大小写。

        上游按注册名精确匹配（区分大小写），而客户端习惯写小写。查不到就原样透传
        （交给上游判定，不凭本地表拒请求）。
        """
        key = (model or "").strip().lower()
        if not key:
            return model
        with self._lock:
            return self._known.get(key, model)

    def known_models(self) -> list:
        with self._lock:
            return sorted(set(self._known.values()), key=lambda item: (item.lower(), item))

    def catalog_has(self, uid: str, model: str) -> bool:
        """账号目录里是否登记了该模型（目录未知时返回 True，乐观放行）。"""
        key = (model or "").strip().lower()
        with self._lock:
            catalog = self._catalogs.get(uid)
            if catalog is None:
                return True
            return bool(catalog["known"].get(key))

    def clear(self) -> None:
        with self._lock:
            self._catalogs.clear()


class ModelService:
    """模型发现（网络）+ 磁盘缓存。

    磁盘缓存沿用基线的 `models-cache.json`（字段不变），作用有二：
      * 冷启动时（上游不可达）仍能列出上次发现的模型，客户端不至于空列表
      * 手工编辑/备份模型列表的老习惯继续有效
    """

    def __init__(self, cfg, registry: ModelRegistry):
        self.cfg = cfg
        self.registry = registry
        self._lock = threading.RLock()
        self._merged: list = []
        self._merged_at = 0.0
        self._last_fail = 0.0
        self._source_fail: dict = {}
        self.auto_claim = bool(getattr(cfg, "benefit_auto_claim", True))

    # --- 磁盘缓存 ---

    def load_disk_cache(self) -> list:
        path = self.cfg.models_cache
        try:
            with open(path, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
        except (OSError, ValueError):
            return []
        models = []
        for entry in cached.get("models") or []:
            if isinstance(entry, dict) and entry.get("id"):
                models.append(ModelInfo.from_cache_entry(entry))
        return models

    def save_disk_cache(self, models: list) -> None:
        path = self.cfg.models_cache
        payload = {
            "updated_at": int(time.time()),
            "models": [info.to_cache_entry() for info in models],
        }
        try:
            # 共享原子写：唯一临时名 + 重试（固定 ".tmp" 在并发保存时会互相踩）
            write_json_atomic(path, payload)
        except OSError as error:
            print(f"[models] 写模型缓存失败: {error}")

    def cached_age(self) -> float:
        try:
            return time.time() - os.path.getmtime(self.cfg.models_cache)
        except OSError:
            return float("inf")

    # --- 来源退避 ---

    def _source_blocked(self, key: str) -> bool:
        until = self._source_fail.get(key, 0)
        if until and time.time() < until:
            return True
        self._source_fail.pop(key, None)
        return False

    def _note_source_fail(self, key: str) -> None:
        self._source_fail[key] = time.time() + SOURCE_FAIL_COOLDOWN

    # --- 发现 ---

    def fetch_models(self, cred: SignCredential, uid: str, timeout: int = 30) -> list:
        """拉取指定账号当前可用模型（三路合并），并写入该账号目录。

        任一路失败只记日志不阻断：福利接口不可用时仍能拿到内置模型。
        """
        sets = []
        try:
            agent_models = self._fetch_agent_models(cred, timeout)
            if agent_models:
                sets.append(agent_models)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            print(f"[models] {uid} agent-center 模型发现失败: {error}")

        builtin_ok = False
        try:
            builtin = self._fetch_builtin_models(cred, uid, timeout)
            builtin_ok = True
            sets.append(builtin)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            print(f"[models] {uid} 内置模型发现失败: {error}")

        benefit_ok = False
        try:
            benefit = self._fetch_benefit_models(cred, uid, timeout)
            benefit_ok = True
            sets.append(benefit)
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            print(f"[models] {uid} 福利模型发现失败: {error}")

        merged = merge_models(*sets)
        if not merged:
            raise ValueError("模型发现返回空列表")
        # 福利来源失败但内置来源成功：保留上一轮已确认的福利条目，别把标记弄丢
        self.registry.set_account_models(uid, merged, keep_benefit=builtin_ok and not benefit_ok)
        return merged

    def _fetch_agent_models(self, cred: SignCredential, timeout: int) -> list:
        """AgentCenter：useragents → detail.gpts.models（基础签名 / agent 通道）。"""
        list_url = f"{SNAP_HOST}{EP_AGENT_LIST}?offset=0&limit=100&is_primary_agent=true"
        headers = {
            "agent-type": "AgentCenter",
            "x-language": "zh-cn",
            "accept": "application/json",
        }
        response = sign_request("GET", list_url, b"", cred, headers, timeout=timeout)
        response.raise_for_status()
        agents = (response.json() or {}).get("agents") or []
        if not agents:
            return []
        # 官方客户端的筛选顺序：CodeAgent + alias 智能体 + show_in_ide → 主 Agent → 第一个
        chosen = None
        for agent in agents:
            alias = (agent.get("alias") or {}).get("alias_zh_cn")
            if agent.get("agent_name") == "CodeAgent" and alias == "智能体" and agent.get("show_in_ide"):
                chosen = agent
                break
        if chosen is None:
            chosen = next((a for a in agents if a.get("is_primary_agent")), agents[0])
        agent_id = chosen.get("agent_id")
        if not agent_id:
            return []

        import urllib.parse as _urlparse

        detail_url = (
            f"{SNAP_HOST}{EP_AGENT_DETAIL}?agent_id={_urlparse.quote(str(agent_id), safe='')}"
        )
        detail_response = sign_request("GET", detail_url, b"", cred, headers, timeout=timeout)
        detail_response.raise_for_status()
        detail = detail_response.json() or {}
        out = []
        for model in (detail.get("gpts") or {}).get("models") or []:
            # 优先 model_id / model_alias（基线抓包确认这两个字段就是上游注册名）；
            # Go 版优先 model_name，实测两者取值一致，这里保留基线顺序以免改变既有行为。
            model_id = model.get("model_id") or model.get("model_alias") or model.get("model_name")
            if not model_id:
                continue
            params = model.get("model_parameters") or {}
            out.append(
                ModelInfo(
                    id=str(model_id),
                    name=str(model.get("model_name") or model_id),
                    context_window=params.get("context_window"),
                    max_tokens=params.get("max_tokens"),
                    desc=str(model.get("model_desc") or ""),
                    channel="agent",
                    supports_images=bool(params.get("supports_images", False)),
                    enable_queue=bool(params.get("enable_queue", False)),
                )
            )
        return out

    def _fetch_builtin_models(self, cred: SignCredential, uid: str, timeout: int) -> list:
        key = f"{uid}|builtin"
        if self._source_blocked(key):
            raise ValueError("内置模型来源处于退避窗口")
        headers = {
            "content-type": "application/json",
            "agent-type": "PromptCenter",
            "x-language": "zh-cn",
        }
        try:
            response = sign_request(
                "GET", SNAP_HOST + EP_MODEL_BUILTIN, b"", cred, headers, timeout=timeout
            )
            response.raise_for_status()
            payload = response.json() or {}
        except Exception:
            self._note_source_fail(key)
            raise
        self._source_fail.pop(key, None)
        out = []
        for model in payload.get("builtinModels") or []:
            model_id = model.get("model_id") or model.get("model_name")
            if not model_id:
                continue
            out.append(
                ModelInfo(
                    id=str(model_id),
                    name=str(model.get("model_name") or model_id),
                    context_window=model.get("context_window"),
                    max_tokens=model.get("max_tokens"),
                    desc=str(model.get("model_desc") or ""),
                    channel="agent",
                )
            )
        return out

    def _fetch_benefit_models(self, cred: SignCredential, uid: str, timeout: int) -> list:
        """限时福利模型（gateway/config），结果整体标记 benefit。

        发现前按配置自动领取：**不领取时福利模型调用必然失败**（上游返回
        `benefit not found`），官方客户端打开模型菜单也会调用这个领取接口（幂等）。
        """
        key = f"{uid}|benefit"
        if self._source_blocked(key):
            raise ValueError("福利模型来源处于退避窗口")
        if self.auto_claim:
            try:
                self.claim_benefit(cred, timeout)
            except Exception as error:
                print(f"[models] {uid} 限时福利领取失败（继续查列表）: {error}")
        headers = {"content-type": "application/json", "x-language": "zh-cn"}
        try:
            response = sign_request(
                "GET", self._benefit_url(EP_BENEFIT_CONFIG), b"", cred, headers, timeout=timeout
            )
            response.raise_for_status()
            payload = response.json() or {}
        except Exception:
            self._note_source_fail(key)
            raise
        error_code = payload.get("error_code")
        if error_code not in (None, "0000", 0, "0"):
            self._note_source_fail(key)
            raise ValueError(f"gateway/config error_code={error_code}")
        self._source_fail.pop(key, None)
        out = []
        for model in ((payload.get("result") or {}).get("models")) or []:
            model_id = model.get("model_id") or model.get("model_name")
            if not model_id:
                continue
            out.append(
                ModelInfo(
                    id=str(model_id),
                    name=str(model.get("model_name") or model_id),
                    context_window=model.get("context_window"),
                    max_tokens=model.get("max_tokens"),
                    desc=str(model.get("model_desc") or "免费额度模型（签到领取）"),
                    channel="benefit",
                    benefit=True,
                )
            )
        return out

    def _benefit_url(self, path: str) -> str:
        from .const import BENEFIT_HOST

        return BENEFIT_HOST + path

    def claim_benefit(self, cred: SignCredential, timeout: int = 30) -> None:
        """领取限时福利（幂等：已领取也返回成功）。"""
        from .const import BENEFIT_HOST

        body = b"{}"
        response = sign_request(
            "POST",
            BENEFIT_HOST + EP_BENEFIT_CLAIM,
            body,
            cred,
            {"content-type": "application/json", "x-language": "zh-cn"},
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise ValueError(f"领取限时福利 HTTP {response.status_code}: {response.text[:200]}")
        payload = response.json() or {}
        if payload.get("error_code") not in (None, "0000", 0, "0"):
            raise ValueError(
                f"领取限时福利失败 error_code={payload.get('error_code')} "
                f"msg={payload.get('error_msg')}"
            )

    # --- 汇总（/v1/models 用） ---

    def refresh_for_pool(self, pool, timeout: int = 30) -> list:
        """刷新各健康账号的模型目录，返回并集（内存缓存 + 磁盘缓存兜底）。

        目录按账号存放（福利按账号授予，账号之间不能互相污染），因此这里逐个健康账号
        发现并写入其目录；发现失败的账号沿用旧目录或磁盘缓存。
        """
        now = time.time()
        with self._lock:
            if self._merged and (now - self._merged_at) < CATALOG_TTL_SECONDS:
                return list(self._merged)
            if self._last_fail and (now - self._last_fail) < FETCH_FAIL_COOLDOWN:
                return list(self._merged) or self.load_disk_cache()

        sets = []
        for account in pool.accounts():
            if account.cred is None or not account.healthy():
                continue
            if not self.registry.account_catalog_stale(account.uid):
                models, known = self.registry.account_models(account.uid)
                if known:
                    sets.append(models)
                    continue
            try:
                sets.append(self.fetch_models(account.cred.sign_credential(), account.uid, timeout))
            except Exception as error:
                print(f"[models] 账号 {account.uid} 模型发现失败: {error}")
                models, known = self.registry.account_models(account.uid)
                if known:
                    sets.append(models)

        merged = merge_models(*sets)
        if not merged:
            # 全部失败：回退磁盘缓存/兜底列表，保证 /v1/models 不空、不崩
            merged = self.load_disk_cache() or [
                ModelInfo(id=model_id, name=model_id,
                          channel="benefit" if model_id in BENEFIT_MODELS else "agent",
                          benefit=model_id in BENEFIT_MODELS)
                for model_id in FALLBACK_MODELS
            ]
            with self._lock:
                self._last_fail = now
                self._merged = list(merged)
                self._merged_at = now
            return list(merged)

        self.save_disk_cache(merged)
        with self._lock:
            self._merged = list(merged)
            self._merged_at = now
            self._last_fail = 0.0
        return list(merged)

    def current(self) -> list:
        """最近一次合并结果（不触发网络）。"""
        with self._lock:
            if self._merged:
                return list(self._merged)
        return self.load_disk_cache()


# ---------------------------------------------------------------------------
# 可用性探测
# ---------------------------------------------------------------------------

def upstream_unavailable_reason(text: str) -> str:
    """把上游错误文本归类为「该模型对这个账号不可用」，否则返回空串。

    这两类错误是**账号能力问题**，不是账号故障：绝不能让它们触发账号冷却。
    """
    low = (text or "").lower()
    if "002002009" in low or "not registered" in low:
        return "模型未在当前账号注册"
    if "4004.200" in low or "benefit not found" in low:
        return "限时福利未领取或已过期（可打开 benefit_auto_claim 或手动签到）"
    return ""


def probe_model(pool, registry: ModelRegistry, account, model: str, cfg) -> None:
    """用一条最小请求真实试一次该模型，并记录结论。

    探测会占用上游会话槽位（单账号只有 3 个并发会话且释放很慢），因此必须：
      * 账号有真实流量在跑或刚跑过 → 整体让位，等下一轮（探测只是让 /v1/models
        说真话，绝不能因此让用户请求排队等锁）
      * 走账号的并发槽位（拿不到就跳过）
    """
    # 延迟导入：upstream 需要 registry 判断 benefit 路由，模块级互相 import 会成环。
    from . import upstream as upstream_mod

    if account is None or account.cred is None:
        return
    if pool.busy(account.uid) or registry.availability.traffic_idle_seconds(account.uid) < (
        cfg.availability.probe_idle_before_minutes * 60
    ):
        return
    if not pool.acquire(account.uid):
        return
    try:
        if not registry.availability.begin_probe(account.uid, model):
            return
        try:
            client = upstream_mod.UpstreamClient(cfg, registry)
            outcome = client.probe(account, model, cfg.availability.probe_timeout_seconds)
        finally:
            registry.availability.end_probe(account.uid, model)
        if outcome is None:
            return
        usable, reason = outcome
        if usable:
            registry.availability.mark_usable(account.uid, model)
            print(f"[probe] {account.uid}/{model}: 可用")
        else:
            registry.availability.mark_unusable(account.uid, model, reason)
            print(f"[probe] {account.uid}/{model}: 不可用：{reason}")
    finally:
        pool.release(account.uid)


def pending_probes(pool, registry: ModelRegistry) -> list:
    """列出所有「健康账号 × 已发现但还没有可用性结论」的组合。"""
    out = []
    for account in pool.accounts():
        if account.cred is None or not account.healthy():
            continue
        models, known = registry.account_models(account.uid)
        if not known:
            continue
        for info in models:
            state, _ = registry.availability.get(account.uid, info.id)
            if state == AVAIL_UNKNOWN:
                out.append((account, info.id))
    return out


def model_known_unusable(pool, registry: ModelRegistry, model: str) -> bool:
    """该模型是否在**所有**健康账号上都被判定为不可用。

    只要有一个账号没结论或可用就返回 False（列表保留该模型）—— 宁可让用户试，
    也别把可用模型藏起来。
    """
    any_known = False
    all_unusable = True
    for account in pool.accounts():
        if account.cred is None or not account.healthy():
            continue
        state, _ = registry.availability.get(account.uid, model)
        if state == AVAIL_UNKNOWN:
            return False
        any_known = True
        if state != AVAIL_UNUSABLE:
            all_unusable = False
    return any_known and all_unusable
