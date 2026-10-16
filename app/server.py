# -*- coding: utf-8 -*-
"""Flask 应用：OpenAI 兼容路由 + 管理 API + WebUI。

路由清单（`/v1/*` 与 `/admin/api/*` 全部要求 `Authorization: Bearer <api_key>`）：

    GET  /v1/models                     模型列表（只列真实可用的）
    GET  /v1/models/<id>                单模型详情
    POST /v1/chat/completions           对话（流式/非流式，多账号轮转）
    GET  /healthz                       存活探针（不鉴权，供容器/反代用）
    GET  /                               WebUI（页面本身匿名，操作要 key）
    GET  /admin/api/overview            总览
    POST /admin/api/credits             校验账号
    POST /admin/api/checkin             签到（积分活动 + opengw 免费额度）
    POST /admin/api/keepalive           保活 Token（续期 + 轻量请求）
    POST /admin/api/reload              重载 auths
    POST /admin/api/accounts/enable|disable|clear-cooldown|delete
    POST /admin/api/oauth/start|poll|import-callback
    GET  /oauth/callback                OAuth 回调（本机浏览器直连时用）

**签到与保活是两个不同的接口、不同的按钮文案**：`/admin/api/checkin` 领积分，
`/admin/api/keepalive` 只续期凭证。Go 版因为共用模板把这两个按钮的文案弄混过，
本实现从 API 到文案都严格分开。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, request

from . import __version__
from . import models as models_mod
from . import oauth as oauth_mod
from . import store as store_mod
from . import upstream as upstream_mod
from .pool import CoolKind, Pool
from .scheduler import Scheduler, build_login_config

# 模型列表的内存缓存时长：客户端（Cherry Studio 等）会频繁轮询 /v1/models，
# 每次请求都去打上游会把账号并发槽位占满。
MODEL_LIST_TTL = 300.0
# 单次请求最多轮换的账号数上限兜底（防止 max_rotate 被配成极大值）
MAX_ROTATE_GUARD = 32


class AppState:
    """进程级共享状态（Flask 多线程下由各组件自己的锁保护）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.credentials = store_mod.load_accounts(cfg)
        self.pool = Pool(cfg, self.credentials, store_mod.StateStore(cfg.state_file))
        self.registry = models_mod.ModelRegistry(
            models_mod.AvailabilityStore(ttl_seconds=cfg.availability.ttl_minutes * 60)
        )
        self.model_service = models_mod.ModelService(cfg, self.registry)
        self.chat_store = store_mod.ChatStore(cfg.chats_file)
        self.oauth_sessions = oauth_mod.OAuthSessionStore(ttl=cfg.oauth_session_ttl)
        self.login_config = build_login_config(cfg, self.credentials)
        self.scheduler = Scheduler(cfg, self.pool, self.registry, self.model_service)
        self._models_lock = threading.RLock()
        self._models: list = []
        self._models_at = 0.0
        # 供 /oauth/callback 定位 pending 会话：ticket_id → session_id
        self.pending_logins: dict = {}

    # --- 模型 ---

    def available_models(self, force: bool = False) -> list:
        """返回当前账号池可用模型的并集（内存缓存 5 分钟）。

        池里一个账号都没有时直接返回空列表：磁盘缓存/兜底列表里的模型在当前部署下
        根本没有账号能服务，列出来只会误导客户端（SPEC 也要求「无账号时返回空列表
        且不崩」）。有账号但上游不可达时才回退缓存 —— 那是基线行为，必须保留。
        """
        if len(self.pool) == 0:
            return []
        now = time.time()
        with self._models_lock:
            if not force and self._models and (now - self._models_at) < MODEL_LIST_TTL:
                return list(self._models)
        models = self.model_service.refresh_for_pool(self.pool)
        # 过滤掉「所有健康账号都判定不可用」的模型：这些条目点开就是 404 / benefit not
        # found，列出来只会误导。未知（还没探测出结论）的保留 —— 宁可让用户试，
        # 也别把可用模型藏起来。
        kept = [
            info
            for info in models
            if not models_mod.model_known_unusable(self.pool, self.registry, info.id)
        ]
        if kept:
            models = kept
        with self._models_lock:
            self._models = list(models)
            self._models_at = now
        return list(models)

    def find_model(self, requested: str):
        """按大小写不敏感查模型条目。"""
        if not requested:
            return None
        lowered = requested.lower()
        for info in self.available_models():
            if info.id.lower() == lowered:
                return info
        return None

    def model_is_known(self, requested: str) -> bool:
        lowered = (requested or "").lower()
        return any(item.lower() == lowered for item in self.registry.known_models())

    def reload_credentials(self) -> dict:
        self.credentials = store_mod.load_accounts(self.cfg)
        summary = self.pool.sync(self.credentials)
        self.login_config = build_login_config(self.cfg, self.credentials)
        return summary


# ---------------------------------------------------------------------------
# 响应辅助
# ---------------------------------------------------------------------------

def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Codearts-Chat-Id"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "X-Codearts-Chat-Id"
    return resp


def openai_error(message: str, status: int = 400, code: str = "invalid_request_error", param: str = ""):
    payload = {"error": {"message": message, "type": "invalid_request_error", "code": code}}
    if param:
        payload["error"]["param"] = param
    return _cors(jsonify(payload)), status


def ok_json(payload: dict, status: int = 200):
    return _cors(jsonify(payload)), status


def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(cfg) -> Flask:
    state = AppState(cfg)
    app = Flask(__name__)
    app.config["CA2A_STATE"] = state

    @app.after_request
    def _after(resp: Response) -> Response:  # noqa: ANN001
        return _cors(resp)

    def require_key():
        """API Key 校验。返回 None 表示通过，否则返回响应元组。"""
        expected = cfg.api_key
        if not expected:
            # 启动阶段已经拒绝「没有 api_key」，这里只是双保险
            return openai_error("服务未配置 API Key，拒绝访问", 401, "invalid_api_key")
        token = _bearer_token()
        if not token or not hmac.compare_digest(token, expected):
            return openai_error("缺少或无效的 API Key", 401, "invalid_api_key")
        return None

    def guarded(handler):
        """给路由加 API Key 校验的装饰器。"""

        def wrapper(*args, **kwargs):
            rejected = require_key()
            if rejected is not None:
                return rejected
            return handler(*args, **kwargs)

        wrapper.__name__ = handler.__name__
        return wrapper

    # ------------------------------------------------------------------
    # WebUI
    # ------------------------------------------------------------------

    panel_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "panel.html")

    def serve_panel():
        try:
            with open(panel_path, "r", encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            return _cors(
                Response(
                    "<h3>panel.html 缺失</h3><p>请确认 app/web/panel.html 存在。</p>",
                    mimetype="text/html",
                )
            )
        resp = Response(html, mimetype="text/html")
        # 面板是单文件、随代码发布；禁缓存避免升级后浏览器还在用旧页面
        resp.headers["Cache-Control"] = "no-store"
        return _cors(resp)

    for route in ("/", "/admin", "/panel", "/panel/"):
        app.add_url_rule(route, f"panel_{route}", serve_panel, methods=["GET"])

    @app.route("/healthz", methods=["GET"])
    def healthz():
        stats = state.pool.stats()
        return _cors(
            jsonify(
                {
                    "ok": True,
                    "service": "codearts2api",
                    "version": __version__,
                    "accounts": stats,
                    "watch": state.scheduler.status(),
                }
            )
        )

    # ------------------------------------------------------------------
    # OpenAI 兼容
    # ------------------------------------------------------------------

    @app.route("/v1/models", methods=["GET", "OPTIONS"])
    def list_models():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        models = state.available_models()
        return _cors(
            jsonify({"object": "list", "data": [info.to_openai_payload() for info in models]})
        )

    @app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
    def get_model(model_id):
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        info = state.find_model(model_id)
        if info is None:
            return openai_error(f"model '{model_id}' not found", 404, "model_not_found")
        return _cors(jsonify(info.to_openai_payload()))

    @app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
    def chat_completions():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        return _handle_chat(state)

    @app.route("/status", methods=["GET"])
    def status():
        rejected = require_key()
        if rejected is not None:
            return rejected
        return _cors(jsonify({"accounts": state.pool.list_view()}))

    # ------------------------------------------------------------------
    # 管理 API
    # ------------------------------------------------------------------

    @app.route("/admin/api/overview", methods=["GET", "OPTIONS"])
    def admin_overview():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        models = state.available_models()
        return _cors(
            jsonify(
                {
                    "service": "codearts2api",
                    "version": __version__,
                    "mode": cfg.mode,
                    "region": "cn",
                    "stats": state.pool.stats(),
                    "accounts": state.pool.list_view(),
                    "models": [
                        {"id": info.id, "benefit": bool(info.benefit)} for info in models
                    ],
                    "schedule": {"watch": state.scheduler.status()},
                }
            )
        )

    def _read_uid_body() -> dict:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        uid = str(payload.get("uid") or payload.get("account") or "").strip()
        return {
            "uid": uid,
            "reason": str(payload.get("reason") or "").strip(),
        }

    def _targets(uid: str) -> list:
        if uid:
            return [uid] if state.pool.get(uid) is not None else []
        return [account.uid for account in state.pool.accounts() if not account.disabled]

    def _summary(action: str, results: list) -> str:
        if not results:
            return f"{action}: 无目标账号"
        ok_count = sum(1 for item in results if item.get("ok"))
        fail = len(results) - ok_count
        if fail == 0:
            return f"{action}完成: {ok_count} 成功"
        return f"{action}完成: {ok_count} 成功 / {fail} 失败"

    @app.route("/admin/api/credits", methods=["POST", "OPTIONS"])
    def admin_credits():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        targets = _targets(body["uid"])
        if body["uid"] and not targets:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        results = []

        def check(uid: str) -> dict:
            account = state.pool.get(uid)
            if account is None:
                return {"uid": uid, "ok": False, "message": "account not found"}
            valid, error = state.pool.validate(account, force=True)
            if error is not None:
                return {"uid": uid, "ok": False, "message": str(error)}
            if not valid:
                return {"uid": uid, "ok": False, "message": "凭证无效，需要重新登录"}
            # 顺手刷新可用额度：这是用户点「校验」时最想看到的数字。
            # 额度查询失败不影响校验结论（refresh_quota 内部已隔离）。
            quota = state.pool.refresh_quota(uid, force=True)
            result = {
                "uid": uid,
                "ok": True,
                "message": f"ok（token 剩余 {account.cred.remaining_text()}）",
            }
            if quota:
                result["quota"] = quota
                if quota.get("total_quota"):
                    # 别用 :g —— 大数会变成 9.99546e+06 这种科学计数，面板上很难读
                    balance = quota["total_balance"]
                    total = quota["total_quota"]
                    result["message"] += (
                        f"，可用额度 {int(balance):,} / {int(total):,}"
                    )
            return result

        # 并发校验：账号多时串行会超过客户端超时；限制 5 并发避免把上游打爆
        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(check, targets))
        return ok_json(
            {"ok": all(item["ok"] for item in results), "message": _summary("校验账号", results), "results": results}
        )

    @app.route("/admin/api/quota", methods=["POST", "OPTIONS"])
    def admin_quota():
        """刷新可用额度（只查余额，**不校验凭证**）。

        为什么单独开一个：面板的「刷新」按钮原来只重取 `/admin/api/overview`，
        而 overview 里的额度是账号上的**缓存快照**，于是点刷新数字永远不动。
        「校验账号」虽然会顺带刷额度，但它是重操作（每个账号一次真实上游请求 +
        凭证校验），拿它当刷新用既慢又容易触发风控。

        这里有 10 秒地板（Pool.refresh_quota 的 min_force_interval），
        连点不会把上游打爆；额度查询失败也不影响账号健康状态。
        """
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        targets = _targets(body["uid"])
        if body["uid"] and not targets:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        results = []
        for uid in targets:
            quota = state.pool.refresh_quota(uid, force=True)
            account = state.pool.get(uid)
            message = ""
            if quota.get("error"):
                message = f"查询失败: {quota['error']}"
            elif quota.get("total_quota"):
                message = (f"可用额度 {int(quota['total_balance']):,} / "
                           f"{int(quota['total_quota']):,}")
            else:
                message = "无额度数据"
            results.append({
                "uid": uid,
                "ok": not quota.get("error"),
                "message": message,
                "quota": quota,
                "nickname": account.user_name if account else "",
            })
        return ok_json({
            "ok": all(item["ok"] for item in results),
            "message": _summary("刷新额度", results),
            "results": results,
        })

    @app.route("/admin/api/checkin", methods=["POST", "OPTIONS"])
    def admin_checkin():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if body["uid"] and state.pool.get(body["uid"]) is None:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        results = []
        for uid in _targets(body["uid"]):
            result = state.pool.checkin(uid)
            results.append(
                {
                    "uid": uid,
                    "ok": bool(result.get("ok")),
                    "credits": result.get("credits") or 0,
                    "message": result.get("message") or "",
                    "campaigns": result.get("campaigns") or [],
                    "benefit": result.get("benefit") or {},
                    # Pool.checkin 会顺带刷新可用额度快照，别在这里把它丢掉
                    "quota": result.get("quota") or {},
                }
            )
        return ok_json(
            {"ok": all(item["ok"] for item in results), "message": _summary("签到", results), "results": results}
        )

    @app.route("/admin/api/keepalive", methods=["POST", "OPTIONS"])
    def admin_keepalive():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if body["uid"] and state.pool.get(body["uid"]) is None:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        results = []
        for uid in _targets(body["uid"]):
            result = state.pool.keepalive(uid)
            results.append(
                {
                    "uid": uid,
                    "ok": bool(result.get("ok")),
                    "message": result.get("message") or "",
                }
            )
        return ok_json(
            {"ok": all(item["ok"] for item in results), "message": _summary("保活 Token", results), "results": results}
        )

    @app.route("/admin/api/reload", methods=["POST", "OPTIONS"])
    def admin_reload():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        summary = state.reload_credentials()
        stats = state.pool.stats()
        return ok_json(
            {
                "ok": True,
                "message": (
                    f"已重载 auths：新增 {len(summary['added'])} / 更新 {len(summary['updated'])} "
                    f"/ 移除 {len(summary['removed'])}"
                ),
                "loaded": len(state.credentials),
                "total": stats["total"],
                "healthy": stats["healthy"],
                "detail": summary,
            }
        )

    @app.route("/admin/api/accounts/enable", methods=["POST", "OPTIONS"])
    def admin_enable():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if not body["uid"]:
            return ok_json({"ok": False, "message": "uid required"}, 400)
        if not state.pool.enable(body["uid"]):
            return ok_json({"ok": False, "message": "account not found"}, 404)
        return ok_json({"ok": True, "message": f"已启用 {body['uid']}"})

    @app.route("/admin/api/accounts/disable", methods=["POST", "OPTIONS"])
    def admin_disable():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if not body["uid"]:
            return ok_json({"ok": False, "message": "uid required"}, 400)
        if state.pool.get(body["uid"]) is None:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        state.pool.disable(body["uid"], body["reason"] or "manual disable from panel")
        return ok_json({"ok": True, "message": f"已禁用 {body['uid']}"})

    @app.route("/admin/api/accounts/clear-cooldown", methods=["POST", "OPTIONS"])
    def admin_clear_cooldown():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if not body["uid"]:
            return ok_json({"ok": False, "message": "uid required"}, 400)
        if not state.pool.clear_cooldown(body["uid"]):
            return ok_json({"ok": False, "message": "account not found"}, 404)
        return ok_json({"ok": True, "message": f"已清冷却 {body['uid']}"})

    @app.route("/admin/api/accounts/delete", methods=["POST", "OPTIONS"])
    def admin_delete():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        body = _read_uid_body()
        if not body["uid"]:
            return ok_json({"ok": False, "message": "uid required"}, 400)
        account = state.pool.get(body["uid"])
        if account is None:
            return ok_json({"ok": False, "message": "account not found"}, 404)
        if account.cred is not None and not account.cred.persistent:
            return ok_json(
                {
                    "ok": False,
                    "message": "该账号来自 .env 的 CODEARTS_AK/SK，没有凭证文件可删除；"
                    "请从 .env 移除后重启服务",
                },
                400,
            )
        state.pool.delete(body["uid"])
        return ok_json({"ok": True, "message": f"已删除 {body['uid']}（凭证文件已移除）"})

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _save_login_result(result, verifier: str, dpop_jwk: dict):
        """落盘 auth 并加入账号池。返回 Credential。"""
        cred = store_mod.Credential(
            uid=result.user_id,
            user_name=result.user_name,
            domain_id=result.domain_id,
            mode=store_mod.MODE_OAUTH,
            access_key_id=result.access_key_id,
            secret_access_key=result.secret_access_key,
            security_token=result.security_token,
            expiration=result.expiration,
            refresh_token=result.refresh_token,
            code_verifier=verifier,
            client_id=state.login_config.client_id,
            dpop_private_key=dict(dpop_jwk or {}),
        )
        # portal 回调只回传 code，不带身份；user_id 为空会让文件名退化成
        # codearts-unknown.json，账号池按 uid 建索引就加载不到。
        if not cred.uid or not cred.user_name:
            uid, name, domain = oauth_mod.lookup_identity(cred.sign_credential())
            if not uid:
                uid = oauth_mod.identity_from_refresh_token(result.refresh_token)
            if uid:
                cred.uid = uid
                cred.user_name = name or cred.user_name
                cred.domain_id = domain or cred.domain_id
        if not cred.uid:
            cred.uid = "unknown-" + oauth_mod.random_hex(4)
        os.makedirs(cfg.auth_dir, exist_ok=True)
        cred.path = os.path.join(cfg.auth_dir, cred.file_name())
        cred.save()
        account = state.pool.add_credential(cred)
        state.credentials = store_mod.load_accounts(cfg)
        state.login_config = build_login_config(cfg, state.credentials)
        return cred, account

    @app.route("/admin/api/oauth/start", methods=["POST", "OPTIONS"])
    def admin_oauth_start():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        session = oauth_mod.start_session(
            state.oauth_sessions,
            state.login_config,
            cfg.listen_port,
            callback_host=cfg.oauth_callback_host,
        )
        state.pending_logins[session.ticket_id] = session.session_id
        return ok_json(
            {
                "ok": True,
                "session_id": session.session_id,
                "auth_url": session.auth_url,
                "expires_in": int(cfg.oauth_session_ttl),
                "message": "请在浏览器打开授权链接；若回调打不到本服务，把地址栏里的回调地址粘贴回来",
            }
        )

    @app.route("/admin/api/oauth/poll", methods=["POST", "OPTIONS"])
    def admin_oauth_poll():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        payload = request.get_json(silent=True) or {}
        session_id = str(payload.get("session_id") or request.args.get("session_id") or "")
        if not session_id:
            return ok_json({"ok": False, "status": "error", "message": "session_id required"}, 400)
        session = state.oauth_sessions.get(session_id)
        if session is None:
            return ok_json(
                {"ok": False, "status": "error", "message": "会话不存在或已过期，请重新发起授权"},
                404,
            )
        if session.done:
            if session.error:
                return ok_json({"ok": False, "status": "error", "message": session.error})
            return ok_json(
                {
                    "ok": True,
                    "status": "done",
                    "message": f"登录成功：{session.account_name or session.account_uid}",
                    "account": {"uid": session.account_uid, "nickname": session.account_name},
                }
            )
        # 新式 OAuth 必须等授权码；只有 portal 明确回调过 secret 时才走 ticket 回退，
        # 否则并行轮询会抢先保存一份不含 refresh_token 的临时凭据。
        if not session.ticket_fallback:
            return ok_json(
                {"ok": True, "status": "pending", "message": "等待浏览器返回 OAuth 授权码…"}
            )
        result = oauth_mod.poll_ticket(state.login_config, session.ticket_id, session.secret)
        if result is None:
            return ok_json({"ok": True, "status": "pending", "message": "等待浏览器完成登录…"})
        try:
            cred, _ = _save_login_result(result, session.verifier, session.dpop_private_key)
        except Exception as error:  # 落盘失败也要让前端看到原因
            session.done = True
            session.error = str(error)
            return ok_json({"ok": False, "status": "error", "message": str(error)})
        session.done = True
        session.account_uid = cred.uid
        session.account_name = cred.user_name
        state.oauth_sessions.delete(session_id)
        return ok_json(
            {
                "ok": True,
                "status": "done",
                "message": f"登录成功：{cred.user_name or cred.uid}",
                "account": {"uid": cred.uid, "nickname": cred.user_name},
            }
        )

    @app.route("/admin/api/oauth/import-callback", methods=["POST", "OPTIONS"])
    def admin_oauth_import_callback():
        if request.method == "OPTIONS":
            return _cors(Response(""))
        rejected = require_key()
        if rejected is not None:
            return rejected
        payload = request.get_json(silent=True) or {}
        session_id = str(payload.get("session_id") or "").strip()
        callback_url = str(payload.get("callback_url") or "").strip()
        if not session_id or not callback_url:
            return ok_json({"ok": False, "message": "session_id 和 callback_url 必填"}, 400)
        session = state.oauth_sessions.get(session_id)
        if session is None:
            return ok_json(
                {"ok": False, "message": "会话不存在或已过期，请重新发起授权"}, 404
            )
        valid, message, parsed = oauth_mod.validate_callback_url(callback_url, session)
        if not valid:
            return ok_json({"ok": False, "message": message}, 400)
        query = {key: values[0] for key, values in _parse_query(parsed.query).items() if values}

        code = query.get("code") or ""
        if code:
            try:
                result = oauth_mod.exchange_code(
                    state.login_config, code, session.verifier, session.port, session.dpop_private_key
                )
                cred, _ = _save_login_result(result, session.verifier, session.dpop_private_key)
            except oauth_mod.OAuthError as error:
                return ok_json({"ok": False, "status": "error", "message": str(error)}, 502)
            session.done = True
            session.account_uid = cred.uid
            session.account_name = cred.user_name
            state.oauth_sessions.delete(session_id)
            return ok_json(
                {
                    "ok": True,
                    "status": "done",
                    "message": "OAuth 登录成功，已保存可自动续期凭据",
                    "account": {"uid": cred.uid, "nickname": cred.user_name},
                }
            )

        # 首次回调：带 secret + redirect，把它记下来开启 ticket 轮询通道
        secret = query.get("secret") or ""
        redirect = query.get("redirect") or ""
        if not secret or not oauth_mod.validate_redirect_url(redirect, session):
            return ok_json(
                {"ok": False, "message": "回调地址缺少当前会话的授权信息（secret/redirect 不匹配）"},
                400,
            )
        state.oauth_sessions.enable_ticket_fallback(session_id=session_id, secret=secret)
        return ok_json(
            {
                "ok": True,
                "status": "continue",
                "next_url": redirect,
                "message": "回调已接收，请继续在浏览器打开下面的地址完成授权，然后点「我已授权」",
            }
        )

    @app.route("/oauth/callback", methods=["GET", "POST"])
    def oauth_callback():
        """本机浏览器直连本服务时的回调入口。

        portal 会回调两次：第一次带 secret + redirect（必须 307 跳回 redirect，
        否则登录链路中断，带 code 的第二次回调永远不会到达）；第二次带 code。
        """
        args = request.args if request.method == "GET" else request.form
        code = (args.get("code") or "").strip()
        secret = (args.get("secret") or "").strip()
        redirect = (args.get("redirect") or "").strip()
        ticket_id = (args.get("ticket_id") or "").strip()
        print(
            f"[oauth] 回调命中: has_code={bool(code)} has_secret={bool(secret)} "
            f"has_redirect={bool(redirect)}"
        )
        if secret and redirect:
            # 用 redirect 里的 ticket_id 定位 pending 会话，并把华为云下发的 secret 换进去
            parsed = _parse_query(_urlparse_query(redirect))
            tid = (parsed.get("ticket_id") or [""])[0] or ticket_id
            if tid:
                state.oauth_sessions.enable_ticket_fallback(ticket_id=tid, secret=secret)
                session_id = state.pending_logins.get(tid)
                if session_id:
                    state.oauth_sessions.enable_ticket_fallback(session_id=session_id, secret=secret)
            return _cors(Response("", status=307, headers={"Location": redirect}))

        if not code:
            return _cors(
                Response(
                    "<h3>登录失败：缺少 code</h3><p>请回到 WebUI 重新发起登录。</p>",
                    status=400,
                    mimetype="text/html",
                )
            )

        candidates = state.oauth_sessions.code_candidates(ticket_id=ticket_id, secret=secret)
        if not candidates:
            # 浏览器在远端时 code 通道不可用，提示改用 ticket 通道
            return _cors(
                Response(
                    "<h3>登录已提交，请回到 WebUI 等待结果。</h3>",
                    mimetype="text/html",
                )
            )
        # portal 的回调可能不带任何配对信息（实测只带 code），因此逐个候选尝试。
        # 授权码与 PKCE verifier 一对一校验：用错只会 STS5.1805，不会消耗授权码。
        last_error = None
        for candidate in candidates:
            try:
                result = oauth_mod.exchange_code(
                    state.login_config, code, candidate.verifier, candidate.port, candidate.dpop_private_key
                )
            except oauth_mod.OAuthError as error:
                last_error = error
                continue
            try:
                cred, _ = _save_login_result(result, candidate.verifier, candidate.dpop_private_key)
            except Exception as error:
                return _cors(
                    Response(f"<h3>保存账号失败：{error}</h3>", status=502, mimetype="text/html")
                )
            candidate.done = True
            candidate.account_uid = cred.uid
            candidate.account_name = cred.user_name
            return _cors(
                Response(
                    f"<h3>登录成功（{cred.user_name or cred.uid}），可关闭此页面并回到 WebUI。</h3>",
                    mimetype="text/html",
                )
            )
        hint = ""
        text = str(last_error or "")
        if "STS5.1805" in text or "invalid authorization code" in text:
            hint = (
                "<p>授权码已失效：请回到 WebUI 重新点「授权登录」获取新链接，"
                "并确保从生成到完成授权期间服务没有重启。</p>"
            )
        return _cors(
            Response(
                f"<h3>换取凭证失败：{last_error}</h3>{hint}", status=502, mimetype="text/html"
            )
        )

    return app


def _parse_query(raw: str) -> dict:
    import urllib.parse

    return urllib.parse.parse_qs(raw or "", keep_blank_values=True)


def _urlparse_query(url: str) -> str:
    import urllib.parse

    return urllib.parse.urlparse(url or "").query


# ---------------------------------------------------------------------------
# 对话处理
# ---------------------------------------------------------------------------

def _conversation_id(body: dict, headers) -> tuple:
    """确定本次请求的会话 ID 与 chat_id。

    多轮上下文分组：OpenAI 线格式本身不带会话 ID，因此优先用显式来源
    （请求头 `X-Codearts-Chat-Id` / `X-Session-Affinity` / `X-Session-Id`，
    或 body 里的 `conversation_id`）；都没有时用**首条 user 消息的哈希**做锚点，
    这样同一个客户端反复发同一段对话仍能命中同一会话（Agent 工具链的历史会增长，
    但首条 user 消息不变）。

    返回 `(conversation_id, chat_id)`：chat_id 是上游要求的 32 位十六进制。
    """
    explicit = (
        headers.get("X-Codearts-Chat-Id")
        or headers.get("X-Session-Affinity")
        or headers.get("X-Session-Id")
        or str(body.get("conversation_id") or "")
    ).strip()
    if explicit:
        if _is_hex32(explicit):
            return explicit.lower(), explicit.lower()
        return explicit, hashlib.sha256(explicit.encode("utf-8")).hexdigest()[:32]

    messages = body.get("messages") or []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            anchor = content
            if body.get("tools"):
                # OpenCode 会用同一条用户消息先请求无工具的会话标题，再启动工具 Agent。
                # 两者必须分开，否则标题生成会污染内置 Agent 会话。
                anchor += "\x00tools"
            digest = hashlib.sha256(anchor.encode("utf-8")).hexdigest()
            return digest[:32], digest[:32]
    return "", ""


def _is_hex32(value: str) -> bool:
    if len(value) != 32:
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in value)


def _handle_chat(state: AppState):
    """对话主流程：解析 → 校验 → 轮转 → 转发。"""
    cfg = state.cfg
    raw = request.get_data()
    if not raw:
        return openai_error("empty body", 400)
    try:
        body = json.loads(raw)
    except ValueError:
        return openai_error("invalid JSON body", 400)
    if not isinstance(body, dict):
        return openai_error("request body must be a JSON object", 400)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return openai_error("messages 不能为空", 400, param="messages")

    # tool_choice 校验：指定函数不存在或重复定义 → 400（客户端问题，不打上游）
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        return openai_error("tools 必须是数组", 400, param="tools")
    choice = upstream_mod.parse_tool_choice(body.get("tool_choice"))
    try:
        tools, tool_choice = upstream_mod.apply_tool_choice(tools, choice)
    except ValueError as error:
        return openai_error(str(error), 400, param="tool_choice")

    requested_model = body.get("model") or cfg.default_model
    if requested_model == "auto":
        requested_model = cfg.default_model
    info = state.find_model(requested_model)
    if info is None and not state.model_is_known(requested_model):
        return openai_error(
            f"model '{requested_model}' 不在当前账号可用模型列表中", 404, "model_not_found", "model"
        )
    model = info.id if info is not None else state.registry.canonical_model(requested_model)

    # 所有健康账号都判定用不了这个模型 → 直接快速失败，不必轮转一遍拿同样的 404
    if models_mod.model_known_unusable(state.pool, state.registry, model):
        return openai_error(
            f"model {model} is not available on any configured account", 404, "model_not_available"
        )

    conversation_id, chat_id = _conversation_id(body, request.headers)
    want_stream = bool(body.get("stream", False))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    sticky_uid = state.chat_store.account_for(conversation_id) if conversation_id else ""

    tried: set = set()
    attempts = 0
    last_error = None
    guard = 0
    guard_limit = MAX_ROTATE_GUARD + len(state.pool)

    while attempts < cfg.max_rotate and guard < guard_limit:
        guard += 1
        account = None
        if sticky_uid and sticky_uid not in tried:
            candidate = state.pool.get(sticky_uid)
            if candidate is not None and candidate.healthy():
                account = candidate
        if account is None:
            account = state.pool.pick_excluding(tried)
        if account is None:
            break
        tried.add(account.uid)

        # 满并发 → 跳过该账号且**不计入重试轮数**（SPEC 要求）；tried 保证不会死循环
        if not state.pool.acquire(account.uid):
            last_error = last_error or upstream_mod.QueueLimitError(
                f"账号 {account.uid} 并发已满"
            )
            continue

        attempts += 1
        try:
            outcome = _try_account(
                state,
                account,
                body,
                model,
                tools,
                tool_choice,
                chat_id,
                conversation_id,
                want_stream,
                include_usage,
            )
        finally:
            state.pool.release(account.uid)

        if outcome["done"]:
            return outcome["response"]
        if outcome.get("sticky_failed"):
            sticky_uid = ""
        last_error = outcome.get("error") or last_error

    message = "all accounts unavailable (disabled/cooldown)"
    if last_error is not None:
        message += f": {last_error}"
    return openai_error(message, 503, "no_healthy_account")


def _try_account(
    state: AppState,
    account,
    body: dict,
    model: str,
    tools,
    tool_choice: str,
    chat_id: str,
    conversation_id: str,
    want_stream: bool,
    include_usage: bool,
):
    """对单个账号尝试一次完整请求。

    返回 `{"done": True, "response": ...}` 表示已经产生最终响应；否则返回
    `{"done": False, "error": ...}`，由调用方决定是否换下一个账号。
    """
    cfg = state.cfg
    pool = state.pool

    valid, error = pool.validate(account)
    if error is not None:
        return {"done": False, "error": error}
    if not valid:
        return {"done": False, "error": upstream_mod.AuthFailedError("凭证无效")}

    canonical = state.registry.canonical_model(model)
    benefit = state.registry.is_benefit_model(account.uid, model)
    cred = account.cred.sign_credential()
    payload = upstream_mod.build_body(
        body,
        model=model,
        canonical_model=canonical,
        chat_id=chat_id,
        tools=tools,
        tool_choice=tool_choice,
    )
    client = upstream_mod.UpstreamClient(cfg, state.registry)
    registry = state.registry
    registry.availability.note_traffic(account.uid)

    response = None
    auth_retried = False
    last_error = None
    for retry in range(cfg.queue.max_attempts):
        try:
            response = client.send(
                cred, payload, benefit=benefit, model_id=canonical
            )
            break
        except upstream_mod.AuthFailedError as error:
            last_error = error
            # 401 且还能刷新 → 刷新后原地重试一次（token 可能刚好在飞行中过期）
            if not auth_retried and account.cred.can_refresh():
                auth_retried = True
                try:
                    pool.refresh(account)
                    cred = account.cred.sign_credential()
                    continue
                except Exception as refresh_error:
                    print(f"[chat] 账号 {account.uid} 401 后刷新失败: {refresh_error}")
            pool.disable(account.uid, f"401 鉴权失效: {error}", invalid=True)
            return {"done": False, "error": error, "sticky_failed": True}
        except upstream_mod.QueueLimitError as error:
            # 429 / 并发会话 / TPM：**不冷却**，原地等待后重试（SPEC 要求）
            last_error = error
            print(
                f"[chat] 上游限流 账号={account.uid} 第 {retry + 1}/{cfg.queue.max_attempts} 次，"
                f"等待 {cfg.queue.retry_seconds}s"
            )
            time.sleep(max(1, cfg.queue.retry_seconds))
            continue
        except upstream_mod.ParamInvalidError as error:
            # 参数错误是客户端问题：换账号也是同样的 400，且绝不能冷却账号
            return {
                "done": True,
                "response": openai_error(
                    f"上游拒绝请求参数: {_truncate(str(error), 200)}", 400, "invalid_request"
                ),
            }
        except upstream_mod.ModelUnavailableError as error:
            # 模型级错误：换账号也是同样结果，直接给客户端明确答复
            registry.availability.mark_unusable(account.uid, model, error.reason)
            return {
                "done": True,
                "response": openai_error(
                    f"model {model} is not available: {_truncate(str(error), 200)}",
                    404,
                    "model_not_available",
                ),
            }
        except upstream_mod.RiskControlError as error:
            # 风控：指数退避，累计到阈值升级为禁用
            pool.note_risk(account.uid, str(error))
            return {"done": False, "error": error, "sticky_failed": True}
        except upstream_mod.UpstreamServerError as error:
            # 5xx：重试已在这一层耗尽（非限流类错误不重试），冷却该账号并换下一个
            pool.cooldown(
                account.uid, CoolKind.ERR, cfg.cooldown.err_cooldown, str(error)
            )
            return {"done": False, "error": error, "sticky_failed": True}
        except upstream_mod.UpstreamError as error:
            last_error = error
            pool.note_error(account.uid, str(error))
            return {"done": False, "error": error, "sticky_failed": True}
        except Exception as error:  # 网络层
            last_error = error
            pool.note_error(account.uid, str(error))
            return {"done": False, "error": error, "sticky_failed": True}

    if response is None:
        return {"done": False, "error": last_error or upstream_mod.QueueLimitError("上游持续限流")}

    registry.availability.mark_usable(account.uid, model)
    registry.availability.note_traffic(account.uid)

    if want_stream:
        def generate():
            try:
                for frame in upstream_mod.stream_frames(
                    response,
                    model,
                    benefit,
                    include_usage=include_usage,
                    messages=body.get("messages"),
                ):
                    yield frame
            finally:
                response.close()
                pool.ping_keepalive(account.uid)
                pool.note_success(account.uid)
                _remember_chat(state, conversation_id, account.uid, chat_id)

        resp = Response(generate(), status=200, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["X-Codearts-Chat-Id"] = chat_id
        return {"done": True, "response": _cors(resp)}

    result = upstream_mod.parse_upstream_json(response)
    response.close()
    embedded = upstream_mod.embedded_error(result)
    if embedded is not None:
        if isinstance(embedded, upstream_mod.ParamInvalidError):
            return {
                "done": True,
                "response": openai_error(
                    f"上游拒绝请求参数: {_truncate(str(embedded), 200)}", 400, "invalid_request"
                ),
            }
        if isinstance(embedded, upstream_mod.ModelUnavailableError):
            registry.availability.mark_unusable(account.uid, model, embedded.reason)
            return {
                "done": True,
                "response": openai_error(
                    f"model {model} is not available: {_truncate(str(embedded), 200)}",
                    404,
                    "model_not_available",
                ),
            }
        pool.note_error(account.uid, str(embedded))
        return {"done": False, "error": embedded, "sticky_failed": True}

    if not result.get("usage"):
        message = (result.get("choices") or [{}])[0].get("message") or {}
        result["usage"] = upstream_mod.estimate_usage(
            body.get("messages"), message.get("content") or "", message.get("reasoning_content") or ""
        )
    result["chat_id"] = chat_id
    pool.note_success(account.uid)
    _remember_chat(state, conversation_id, account.uid, chat_id)
    resp = Response(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status=200,
        content_type="application/json; charset=utf-8",
    )
    resp.headers["X-Codearts-Chat-Id"] = chat_id
    return {"done": True, "response": _cors(resp)}


def _remember_chat(state: AppState, conversation_id: str, uid: str, chat_id: str) -> None:
    """记住「会话 → 账号 / chat_id」，让多轮请求黏在同一账号上。

    只缓存显式会话（有 conversation_id 或请求头）之外的锚点也算显式 —— 锚点是由首条
    user 消息算出来的，天然稳定，值得记住；纯一次性请求（没有 user 消息）不记。
    """
    if not conversation_id:
        return
    state.chat_store.remember(conversation_id, uid, chat_id)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit]
