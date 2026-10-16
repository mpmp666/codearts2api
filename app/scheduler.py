# -*- coding: utf-8 -*-
"""看门狗：token 续期 + 保活 + 自动签到 + 模型可用性探测。

为什么用后台线程而不是 cron：签到与续期都依赖**内存里的池状态**（谁在冷却、谁需要
保活），外部 cron 拿不到这些信息；而且用户期望「服务起来就自动签到一次」，不是
「等服务起来 + 等到下一个 cron 点」。线程里所有异常都被吞掉并记日志 —— 看门狗自己
挂掉会让整个服务慢慢失能（token 过期没人续），所以它必须比业务代码更耐操。

顺序（有意为之）：
  1. **续期**：token 过期了后面所有事都做不了
  2. **保活**：轻量请求，让上游看到会话还活着
  3. **签到**：与推理无关，放最后；它失败绝不影响前两步
  4. **可用性探测**：单独节拍（默认 15 分钟）且启动后延迟 2 分钟，避开接入初期的真实请求
"""
from __future__ import annotations

import threading
import time

from . import models as models_mod
from . import oauth as oauth_mod


class Scheduler:
    """定时任务执行器。"""

    def __init__(self, cfg, pool, registry, model_service):
        self.cfg = cfg
        self.pool = pool
        self.registry = registry
        self.model_service = model_service
        self._thread = None
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self.last_tick_at = 0.0
        self.last_sweep_at = 0.0
        self.last_result: dict = {}
        self.started_at = 0.0
        self.tick_count = 0

    # --- 生命周期 ---

    def start(self) -> None:
        if not self.cfg.watch.enabled:
            print("[scheduler] 看门狗已关闭（CA2A_WATCH_ENABLED=0 / watch.enabled=false）")
            return
        if self._thread is not None:
            return
        self.started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ca2a-watchdog", daemon=True)
        self._thread.start()
        print(
            "[scheduler] 看门狗已启动: poll={}m refresh_skew={}m keepalive={}m "
            "credits_auto_claim={}".format(
                self.cfg.watch.poll_minutes,
                self.cfg.watch.refresh_skew_minutes,
                self.cfg.watch.keepalive_interval_minutes,
                self.cfg.credits_auto_claim,
            )
        )

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _run(self) -> None:
        # 立即执行一次：用户期望「起来就签到」，而不是等 30 分钟
        self.tick()
        interval = max(60, self.cfg.watch.poll_minutes * 60)
        probe_interval = max(300, self.cfg.availability.probe_interval_minutes * 60)
        next_probe = time.time() + 120  # 启动后延迟 2 分钟再开始探测
        while not self._stop.wait(timeout=30):
            now = time.time()
            if now - self.last_tick_at >= interval:
                self.tick()
            if now >= next_probe and now - self.last_sweep_at >= probe_interval:
                self.sweep_availability()
                next_probe = now + probe_interval

    # --- 主循环 ---

    def tick(self) -> dict:
        """一轮：续期 → 保活 → 签到。"""
        if not self._tick_lock.acquire(blocking=False):
            return {"skipped": "上一轮还在跑"}
        try:
            self.last_tick_at = time.time()
            self.tick_count += 1
            summary = {"refreshed": 0, "keepalive": 0, "checkin": 0, "errors": []}
            for account in self.pool.accounts():
                if self._stop.is_set():
                    break
                # 只跳过「被禁用」的账号；**冷却中的账号仍要续期** —— 否则冷却结束时
                # token 已经过期，账号会从「暂时不可用」变成「必须重新登录」。
                if account.disabled:
                    continue
                try:
                    self._tick_account(account, summary)
                except Exception as error:  # 单个账号的异常绝不能终止整轮
                    summary["errors"].append(f"{account.uid}: {error}")
            self.last_result = summary
            return summary
        finally:
            self._tick_lock.release()

    def _tick_account(self, account, summary: dict) -> None:
        ok, error = self.pool.validate(account)
        if error is not None:
            summary["errors"].append(f"{account.uid} 续期失败（可重试）: {error}")
        if not ok:
            return

        # 保活节拍：距上次活动超过 keepalive_interval 才打一次轻量请求
        if (time.time() - account.keepalive_last_ping) >= (
            self.cfg.watch.keepalive_interval_minutes * 60
        ):
            result = self.pool.keepalive(account.uid)
            if result.get("ok"):
                summary["keepalive"] += 1
            else:
                summary["errors"].append(f"{account.uid} 保活失败: {result.get('message')}")

        if self.cfg.credits_auto_claim:
            result = self.pool.checkin(account.uid)
            if result.get("ok"):
                summary["checkin"] += 1
            else:
                summary["errors"].append(f"{account.uid} 签到: {result.get('message')}")
        else:
            # 关掉自动签到时也要能看到「可用额度」：额度查询是只读的，
            # 失败不影响账号健康（refresh_quota 内部已做隔离）。
            self.pool.refresh_quota(account.uid, ttl=3600)

    # --- 可用性探测 ---

    def sweep_availability(self) -> int:
        """对「已发现但还没有可用性结论」的模型做一轮探测。"""
        self.last_sweep_at = time.time()
        pending = models_mod.pending_probes(self.pool, self.registry)
        if not pending:
            return 0
        done = 0
        for account, model in pending:
            if self._stop.is_set():
                break
            try:
                models_mod.probe_model(self.pool, self.registry, account, model, self.cfg)
                done += 1
            except Exception as error:
                print(f"[scheduler] 探测 {account.uid}/{model} 异常: {error}")
            # 探针占用的上游会话槽位释放很慢（实测 >15s），留出释放窗口再探下一个，
            # 否则紧随其后的真实请求会被上游判为 TM.00001041 并发上限。
            if self._stop.wait(timeout=max(0, self.cfg.availability.probe_gap_seconds)):
                break
        return done

    # --- 状态 ---

    def status(self) -> dict:
        return {
            "enabled": bool(self.cfg.watch.enabled),
            "running": bool(self._thread is not None and self._thread.is_alive()),
            "credits_auto_claim": bool(self.cfg.credits_auto_claim),
            "benefit_auto_claim": bool(self.cfg.benefit_auto_claim),
            "poll_minutes": self.cfg.watch.poll_minutes,
            "refresh_skew_minutes": self.cfg.watch.refresh_skew_minutes,
            "keepalive_interval": self.cfg.watch.keepalive_interval_minutes,
            "max_concurrent": self.cfg.max_concurrent,
            "availability_ttl_minutes": self.cfg.availability.ttl_minutes,
            "tick_count": self.tick_count,
            "last_tick_at": _iso(self.last_tick_at),
            "last_sweep_at": _iso(self.last_sweep_at),
            "last_result": self.last_result,
        }


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    import datetime

    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def build_login_config(cfg, credentials: list) -> oauth_mod.LoginConfig:
    """决定 WebUI 登录使用的 OAuth 配置。

    client_id 优先取已有账号记录的取值 —— refresh_token 与 client_id 绑定，
    混用会让老账号刷新失败（`invalid client id`）；其次取配置项，最后才是内置默认值。
    其余字段必须来自默认配置（否则 authorize 链接会缺 host / plugin 信息）。
    """
    login_cfg = oauth_mod.default_login_config(cfg.login_client_id)
    for cred in credentials:
        if cred.client_id:
            login_cfg.client_id = cred.client_id
            break
    return login_cfg
