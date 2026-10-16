# -*- coding: utf-8 -*-
"""CodeArts2API（Python 增强版）应用包。

为什么拆成包：基线把全部逻辑塞在单个 server.py 里（签名、模型发现、路由、SSE 聚合
混在一起），加入 OAuth 登录、多账号池、签到状态机之后单文件会失控。这里按职责拆分，
但入口 `python server.py` 与 `python benefit.py status|claim|models` 的用法保持原样，
老用户的启动脚本与计划任务不需要改。
"""

__all__ = ["__version__"]

# 版本号会出现在 WebUI 与 /healthz 里，方便线上确认跑的是哪一版
__version__ = "2.0.0"
