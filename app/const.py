# -*- coding: utf-8 -*-
"""上游端点与常量（对齐 Go 版 `internal/upstream/const.go` + 基线抓包结果）。

为什么单独一个模块：`signing`（签名）、`upstream`（聊天）、`checkin`（签到）、
`models`（模型发现）、`oauth`（登录）都要引用同一批 host/path。若把它们放在
`upstream.py` 里，其它模块 import 就会和 `upstream` 形成循环依赖，所以常量独立出来。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 主机（来自 Go 版逆向：CodeArts Agent 插件的 product.json）
# ---------------------------------------------------------------------------
# 盘古引擎 / 聊天网关（snap-access）
SNAP_HOST = "https://snap-access.cn-north-4.myhuaweicloud.com"
# 登录 / 令牌服务（snap-manager）
SNAP_MANAGER_HOST = SNAP_HOST + "/snap-manager"
# 华为云 CodeArts 门户（OAuth authorize 入口，注意带 /portal）
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
# STS 临时 AK/SK 换取
STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
# IAM（备用，本实现未使用，保留常量便于排查）
IAM_HOST = "https://iam.myhuaweicloud.com"
# 限时福利（免费套餐）网关
BENEFIT_HOST = "https://opengw.developer.huaweicloud.com"

# OAuth client（官方 CodeArts Agent 插件的 uri_scheme）。
# refresh_token 与该值绑定：换 client_id 刷新会被 STS 拒（invalid client id）。
CLIENT_ID = "codearts-agent"

# 登录插件标识：portal 与 /v1/login/ticket 都按它识别客户端
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"

# ---------------------------------------------------------------------------
# 聊天端点（chatDomain 商业版 = snap-access.cn-north-4）
# ---------------------------------------------------------------------------
# 基线使用的 v2 端点；签名时 CanonicalURI 需要带尾斜杠
EP_CHAT_V2 = "/api/v2/chat/completions"
# AgentCenter 模型发现（老套餐通道）
EP_AGENT_LIST = "/v1/agent-center/agents/useragents"
EP_AGENT_DETAIL = "/v1/agent-center/agents/detail"
# 内置模型（Agent-Type: PromptCenter）
EP_MODEL_BUILTIN = "/v1/model/builtin"

# ---------------------------------------------------------------------------
# 积分活动（签到）—— 位于 snap 根路径，不是 snap-manager
# ---------------------------------------------------------------------------
EP_CREDIT_DELIVERY = "/v1/ops/delivery"
EP_CREDIT_CLAIM = "/v1/ops/claim"
EP_CREDIT_CONFIRM = "/v1/ops/confirm"
CREDIT_CHANNEL = "IDE"

# ---------------------------------------------------------------------------
# 登录 / 令牌
# ---------------------------------------------------------------------------
EP_LOGIN_TICKET = "/v1/login/ticket"
EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_CURRENT_USER = "/v1/current/user"
EP_CALLER_IDENTITY = "/v5/caller-identity"

# ---------------------------------------------------------------------------
# 限时福利网关
# ---------------------------------------------------------------------------
EP_BENEFIT_CONFIG = "/api/v1/gateway/config"
EP_BENEFIT_CLAIM = "/api/v1/benefit/claim"
EP_BENEFIT_BALANCE = "/api/v1/user/tokens/balance"

# ---------------------------------------------------------------------------
# 模型兜底列表（基线抓包确认）
# ---------------------------------------------------------------------------
# 老套餐模型：走 AgentCenter 通道（基础签名）
FALLBACK_AGENT_MODELS = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]
# 免费额度模型：走 benefit 通道，签名时必须额外携带 maas_type / model-id / model-name
BENEFIT_MODELS = ["glm-5.3-flash", "deepseek-v4-pro-0813", "deepseek-v4-flash-0731"]
FALLBACK_MODELS = FALLBACK_AGENT_MODELS + BENEFIT_MODELS

# 限时福利路由头（官方 renderer 检测到 isFreeBenefit 即添加）
HEADER_MAAS_TYPE = "maas_type"
MAAS_BENEFIT = "benefit"
