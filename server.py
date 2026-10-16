# -*- coding: utf-8 -*-
"""CodeArts OpenAI 兼容代理（Python 增强版）入口。

用法（与基线完全一致）：

    pip install -r requirements.txt
    python server.py

默认监听 http://127.0.0.1:8787，OpenAI Base URL: http://127.0.0.1:8787/v1

与基线的区别：启动前会强制校验 API Key 与凭证（缺一个就明确报错退出，而不是裸奔启动），
并拉起看门狗线程做 token 续期 / 保活 / 自动签到。凭证来源可以是 `.env` 的永久 AK/SK，
也可以是 `auths/` 下的 OAuth 凭证（`python login.py` 生成），两者可同时存在。
"""
from __future__ import annotations

import sys

from app import __version__
from app.config import ConfigError, load_config
from app.server import create_app


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as error:
        # 明确报错并退出：缺 key / 缺凭证时绝不能「看起来起来了但谁都能调」
        print("启动失败：", error, file=sys.stderr)
        return 2

    app = create_app(cfg)
    state = app.config["CA2A_STATE"]

    print(f"CodeArts OpenAI 兼容代理 v{__version__} 已启动: http://{cfg.listen_host}:{cfg.listen_port}")
    print(f"OpenAI Base URL: http://{cfg.listen_host}:{cfg.listen_port}/v1")
    print(f"WebUI 控制台:    http://{cfg.listen_host}:{cfg.listen_port}/")
    print(f"凭证模式: {cfg.mode}  账号数: {len(state.pool)}  auth_dir: {cfg.auth_dir}")
    for view in state.pool.list_view():
        flag = "禁用" if view["disabled"] else ("冷却中" if view["cooling"] else "健康")
        print(
            f"  - {view['uid']} ({view['nickname'] or '未命名'}) mode={view['mode']} "
            f"状态={flag} token剩余={view['token_remaining'] or '永久'}"
        )
    if len(state.pool) == 0:
        print("  警告: 当前没有可用账号，/v1/models 会返回空列表；请先运行 python login.py")
    print("按 Ctrl+C 停止")

    # 看门狗：续期 + 保活 + 自动签到（可用 CA2A_WATCH_ENABLED=0 关闭）
    state.scheduler.start()

    try:
        # threaded=True 是必须的：SSE 长连接会占住 worker，单线程会让其他请求全部排队
        app.run(
            host=cfg.listen_host,
            port=cfg.listen_port,
            threaded=True,
            debug=False,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        state.scheduler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
