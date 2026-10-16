# CodeArts2API（Python 增强版）

把华为云 [CodeArts Agent](https://codearts.huaweicloud.com/) 的模型接口包装成**标准
OpenAI Chat Completions API**，供 OpenCode / Cline / Continue / Cherry Studio / LobeChat
等任何支持自定义 OpenAI Base URL 的工具直接使用。

本仓库是 [`Johnsheng1/codearts2api`](https://github.com/Johnsheng1/codearts2api)
（Python/Flask，永久 AK/SK 单账号）的增强实现，补齐了 Go 版
[`HITZY2002/codearts2api`](https://github.com/HITZY2002/codearts2api) 的账号运营能力：
**OAuth 登录、多账号池、token 自动续期、保活、签到自动化、WebUI、模型可用性探测、
API Key 必填、多轮上下文、Docker/systemd**。

底层仍然是华为云 `SDK-HMAC-SHA256` 请求签名，**双通道签名**（agent 基础签名 /
benefit 额外签 `maas_type`+`model-id`+`model-name`）与基线逐字节一致。

> [!TIP]
> 建议使用 opencode、pi、hermes、openclaw 作为智能体接入。免费 CodeArts 具有并发限制，
> 其他代码智能体通常会超过这个限制，从而影响体验。本版本新增了**单账号并发上限**与
> **排队重试**，撞限时会原地等待而不是直接失败。

---

## 1. 与原版 / Go 版的功能对照

| 能力 | 原版（Python 基线） | Go 版 | 本实现 |
|---|---|---|---|
| 凭证模式 | 永久 AK/SK 单账号 | OAuth2 PKCE + DPoP（STS 临时凭证） | **两者并存**（`mode: auto/apikey/oauth`） |
| OAuth 登录 | ✗ | 本机回调 + `--print-only` ticket 轮询 | **本机回调 + `--print-only` + WebUI 发起（含粘贴回调地址）** |
| 多账号池 | ✗ | 轮询 / 冷却 / 禁用 / 并发 | **轮询 / 冷却 / 风控指数退避 / 并发上限 / 会话隔离** |
| token 自动续期 | ✗ | refresh_token 轮转 + 提前 30 分钟刷新 | ✅ 同左，且 DPoP 私钥与 refresh_token 一起落盘复用 |
| 保活 | ✗ | 定期轻量请求（15 分钟） | ✅ 续期 + `GET /v1/current/user` 轻量请求 |
| 签到 | opengw 免费额度（`benefit.py` CLI） | 积分活动（campaign 1/4，claim+confirm） | **两套都有**：积分活动 + opengw 免费额度；自动（调度器）+ 手动（WebUI/API） |
| 限时福利自动领取 | ✗ | `benefit_auto_claim`（默认开） | ✅ 同左 |
| WebUI | ✗ | 单文件面板 | ✅ 单文件、**零 CDN**、深色主题、含对话测试 |
| 签到/保活文案 | — | 共用模板导致「签到」按钮写成「保活」 | **严格区分**：签到绑 `/admin/api/checkin`，保活绑 `/admin/api/keepalive` |
| API Key | 任意非空字符串（等于裸奔） | 必填，缺失拒绝启动 | ✅ 必填，缺失拒绝启动 |
| 模型可用性探测 | ✗（列出未注册模型） | 30 分钟缓存探测 | ✅ 30 分钟缓存 + 真实流量学习 |
| 多轮上下文 | ✗ | `chat_id` 分组 + 请求头/body 指定 | ✅ 同左（`X-Codearts-Chat-Id` / `conversation_id`） |
| 流式 usage | ✗ | `include_usage` → `[DONE]` 前补 usage chunk | ✅ 同左 |
| `tool_choice` | 原样透传 | `auto/none/required/指定函数`（指定函数→只发该函数+`required`） | ✅ 同左，不存在/重复定义 → 400 |
| 部署 | 直接跑 | Docker + compose + systemd | ✅ Docker + docker-compose + systemd unit |
| `benefit.py` CLI | `status`/`claim`/`models` | — | ✅ **用法不变**，且同时支持 OAuth 凭证 |

---

## 2. 快速开始

### 2.1 安装

```bash
pip install -r requirements.txt      # 需要 Python ≥ 3.11
```

### 2.2 配置（两种凭证任选，也可同时用）

```bash
cp .env.example .env
```

**方式 A：永久 AK/SK（基线模式，最少配置）**

```ini
CODEARTS_AK=你的AccessKey
CODEARTS_SK=你的SecretKey
CA2A_API_KEY=用下面命令生成的随机串
```

AK/SK 获取：<https://console.huaweicloud.com/iam/#/mine/accessKey> → 新增访问密钥。

> **请勿泄露你的 AK/SK，它具有华为云账号的最高访问权限。**

**方式 B：OAuth 授权（推荐，可自动续期、可多账号）**

```bash
python login.py                  # 本机有浏览器
python login.py --print-only     # 服务器无浏览器：打印链接，ticket 轮询
```

凭证落盘 `auths/codearts-{user_id}.json`（字段与 Go 版 `internal/auth` 对齐，
两边的 auths 目录可以互相搬运）。

**必填：访问密钥**

```bash
python -c "import secrets;print(secrets.token_hex(24))"
```

写进 `.env` 的 `CA2A_API_KEY`，或 `config.json` 的 `api_key`（env 优先）。
**没有它服务会拒绝启动** —— 基线是任意字符串都能调，等于把接口暴露给任何人。

### 2.3 启动

```bash
python server.py
```

```text
http://127.0.0.1:8787          # WebUI 控制台
http://127.0.0.1:8787/v1       # OpenAI Base URL
```

### 2.4 接入各类工具

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：**必须等于你配置的 `CA2A_API_KEY`**（不再是任意字符串）
- 模型：`/v1/models` 列出的任意一个（如 `GLM-5.2`）

---

## 3. WebUI 控制台

浏览器打开 <http://127.0.0.1:8787/>，输入 API Key 进入。页面本身可匿名访问
（只是静态 HTML），**所有操作都要 Bearer Key**。

包含：账号统计、账号表格（签到结果 / 状态 / 原因 / 冷却至 / 逐账号操作）、
`＋ 授权登录` 弹窗（发起授权 → 复制链接 / 打开授权页 / **提交回调地址** / 我已授权，
自动轮询）、模型列表（带 `福利` 标记）、对话测试（流式 / 非流式）。

顶部按钮与后端接口一一对应：

| 按钮 | 接口 | 说明 |
|---|---|---|
| `＋ 授权登录` | `/admin/api/oauth/start` | 打开 OAuth 弹窗 |
| `刷新` | `/admin/api/overview` | 重新拉总览 |
| `校验账号` | `/admin/api/credits` | 校验凭证 / 触发续期 |
| `全员签到` | `/admin/api/checkin` | 领积分（积分活动 + opengw 免费额度） |
| `保活 Token` | `/admin/api/keepalive` | 只续期凭证 + 轻量请求 |
| `重载 auths` | `/admin/api/reload` | 重新扫描 `auths/` |

> ⚠️ **签到与保活是两件事**，文案与接口严格对应。Go 版因为共用模板把「签到」按钮
> 写成了「保活」，本实现不沿用这个坑。

**浏览器不在服务所在机器上怎么办**：portal 会回调 `http://127.0.0.1:<port>/oauth/callback`，
在远端浏览器里这个地址打不开。此时把地址栏里的**完整回调地址**复制下来，粘贴进
弹窗的输入框点「提交回调地址」——服务端会校验它属于当前会话（只接受 127.0.0.1/localhost
且端口匹配），记录 portal 下发的 `secret` 并开启 ticket 轮询通道，返回下一步地址继续授权。

---

## 4. 配置项

### 4.1 环境变量（`.env`）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `CA2A_API_KEY` | **必填**，访问密钥 | — |
| `CODEARTS_AK` / `CODEARTS_SK` | 永久 AK/SK（基线变量，仍生效） | — |
| `CA2A_MODE` | `auto` / `apikey` / `oauth` | `auto` |
| `CA2A_LISTEN_HOST` | 监听地址 | `127.0.0.1` |
| `CA2A_LISTEN_PORT` | 监听端口 | `8787` |
| `CODEARTS_PROXY_HOST` / `CODEARTS_PROXY_PORT` | 基线变量，同上（仍生效） | — |
| `CA2A_AUTH_DIR` | 凭证目录 | `./auths` |
| `CA2A_STATE_FILE` | 池状态文件 | `./data/state.json` |
| `CA2A_CHATS_FILE` | 会话映射文件（多轮上下文） | `./data/chats.json` |
| `CA2A_MODELS_CACHE` | 模型缓存 | `./models-cache.json` |
| `CA2A_DEFAULT_MODEL` | 默认模型 | `glm-5.2` |
| `CA2A_MAX_ROTATE` | 单请求最多轮换账号数 | `3` |
| `CA2A_MAX_CONCURRENT` | 单账号并发上限（0=不限） | `2` |
| `CA2A_WATCH_ENABLED` | 看门狗开关 | `1` |
| `CA2A_WATCH_POLL_MINUTES` | 看门狗轮询间隔 | `30` |
| `CA2A_WATCH_REFRESH_SKEW` | 到期前多久刷新（分钟） | `30` |
| `CA2A_WATCH_KEEPALIVE_INTERVAL` | 保活间隔（分钟） | `15` |
| `CA2A_KEEPALIVE_WINDOW` | 多久无活动算「需要保活」 | `10m` |
| `CA2A_QUEUE_RETRY_SECONDS` | 上游限流重试间隔 | `10` |
| `CA2A_QUEUE_MAX_ATTEMPTS` | 上游限流重试次数 | `30` |
| `CA2A_CREDITS_AUTO_CLAIM` | 自动签到（积分活动） | `1` |
| `CA2A_BENEFIT_AUTO_CLAIM` | 发现模型时自动领限时福利 | `1` |
| `CA2A_LOGIN_CLIENT_ID` | WebUI 登录的 OAuth client_id | 已有账号取值，否则 `codearts-agent` |
| `CA2A_OAUTH_CALLBACK_HOST` | 远端部署时改写回调端口（必须显式写端口） | — |
| `CA2A_AVAILABILITY_TTL_MINUTES` | 模型可用性结论缓存 | `30` |
| `CA2A_REQUIRE_ACCOUNT` | 启动时强制要求已有账号 | `0` |
| `CA2A_CONFIG` | 指定 config.json 路径 | `./config.json` |

### 4.2 `config.json`

见 [`config.example.json`](config.example.json)（带注释，服务读取时会自动剥离 `//` 与
`/* */`）。主要字段：

| 字段 | 说明 | 默认 |
|---|---|---|
| `listen_host` / `listen_port` | 监听 | `127.0.0.1` / `8787` |
| `api_key` | 访问密钥（env 优先） | `""` |
| `mode` | 凭证来源 | `auto` |
| `auth_dir` / `state_file` / `chats_file` / `models_cache` | 路径 | `./auths` … |
| `default_model` | 默认模型 | `glm-5.2` |
| `max_rotate` | 单请求最多轮换账号数 | `3` |
| `max_concurrent` | 单账号并发上限（0=不限） | `2` |
| `cooldown.err_threshold` | 连续错误阈值 | `3` |
| `cooldown.err_cooldown` | 5xx 冷却时长 | `300s` |
| `risk.base_seconds` / `max_seconds` / `max_strikes` | 风控指数退避（base·2ⁿ⁻¹，上限，升级禁用阈值） | `900` / `86400` / `4` |
| `queue.retry_seconds` / `max_attempts` | 限流原地等待重试 | `10` / `30` |
| `watch.*` | 看门狗（`enabled` / `poll_minutes` / `refresh_skew_minutes` / `keepalive_interval_minutes` / `keepalive_window`） | 见示例 |
| `credits_auto_claim` | 自动签到 | `true` |
| `benefit_auto_claim` | 自动领限时福利 | `true` |
| `availability.*` | 可用性探测（`ttl_minutes` / `probe_interval_minutes` / `probe_idle_before_minutes` / `probe_gap_seconds`） | 见示例 |
| `login_client_id` / `oauth_callback_host` / `oauth_session_ttl` | OAuth | 见示例 |
| `upstream.timeout_seconds` | 上游超时 | `120` |
| `require_accounts` | 启动时强制要求已有账号 | `false` |

---

## 5. 从 AK/SK 迁移到 OAuth

**不需要迁移也能用**：默认 `mode: auto`，`.env` 的 AK/SK 与 `auths/*.json` 会被同时加载，
老部署升级后直接启动即可，行为不变（单账号、永久凭证）。

想切到 OAuth（可自动续期、可多账号）：

1. `python login.py`（或启动后在 WebUI 点「＋ 授权登录」）完成一次授权，
   凭证写入 `auths/codearts-{user_id}.json`。
2. 确认 `/v1/models` 能正常返回，`/admin/api/overview` 里能看到新账号。
3. 可选：把 `.env` 里的 `CODEARTS_AK/SK` 删掉，并把 `mode` 改成 `oauth`，
   让服务只使用 OAuth 凭证（AK/SK 是账号最高权限凭证，不用时删掉更安全）。

几点注意：

- **`refresh_token` 与 `client_id`、DPoP 私钥三者绑定**：凭证文件里会保存
  `client_id` 与 `dpop_private_key`，刷新时原样复用并写回轮转后的新 `refresh_token`。
  手工改过 `login_client_id` 也不影响老账号（它按自己记录的 client_id 刷新）。
- 服务重启会让**未完成的** WebUI 授权会话失效（PKCE verifier 只在内存里）。
  此时回调会提示「授权码已失效，请重新发起」。
- `auths/` 与 `.env` 都已在 `.gitignore` 里，不要提交。

---

## 6. API

### 6.1 OpenAI 兼容

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | 聊天补全（`stream` 流式 / 非流式），多账号自动轮转 |
| `GET /v1/models` | 模型列表（**只列真实可用**的模型） |
| `GET /v1/models/<id>` | 单模型详情（含能力元数据） |
| `GET /status` | 账号池状态快照 |
| `GET /healthz` | 存活探针（**不鉴权**） |

`/v1/*` 全部要求 `Authorization: Bearer <CA2A_API_KEY>`。

**多轮上下文**：优先用请求头 `X-Codearts-Chat-Id`（也接受 `X-Session-Affinity` /
`X-Session-Id`）或 body 里的 `conversation_id`；都没有时用**首条 user 消息的哈希**
作锚点。同一个会话的后续请求会**黏在同一账号**上（否则第二轮会被轮转丢到别的账号，
上游看不到第一轮上下文）。响应头与 body 里都会回 `X-Codearts-Chat-Id` / `chat_id`。

**流式 usage**：请求带 `"stream_options":{"include_usage":true}` 时，会在 `[DONE]`
前额外返回一次 `choices: []` 的 usage chunk；未启用则不返回。上游没给 usage 时按
字符数估算（不是精确 token 计数）。

**`tool_choice`**：支持 `auto` / `none` / `required` / 指定函数对象。指定函数时，
网关只向上游发送该函数的定义，并把 `tool_choice` 转成上游接受的字符串 `required`
（上游 InferHub 不接受对象形式，会报 `InferHub.001001005`）。指定函数不存在或
重复定义返回 **400**，且**不轮换、不冷却账号**。

### 6.2 管理 API

全部要求 Bearer Key：

| 端点 | 说明 |
|---|---|
| `GET /admin/api/overview` | 总览（统计 / 账号 / 模型 / 调度状态） |
| `POST /admin/api/credits` | 校验账号（可传 `{"uid":"..."}`） |
| `POST /admin/api/checkin` | **签到**（不传 uid = 全部启用账号） |
| `POST /admin/api/keepalive` | **保活 Token**（续期 + 轻量请求） |
| `POST /admin/api/reload` | 重载 `auths/` |
| `POST /admin/api/accounts/enable` | `{"uid":...}` |
| `POST /admin/api/accounts/disable` | `{"uid":...,"reason":...}` |
| `POST /admin/api/accounts/clear-cooldown` | `{"uid":...}` |
| `POST /admin/api/accounts/delete` | `{"uid":...}`（删除凭证文件） |
| `POST /admin/api/oauth/start` | 发起授权 → `session_id` + `auth_url` |
| `POST /admin/api/oauth/poll` | `{"session_id":...}` → `pending`/`done`/`error` |
| `POST /admin/api/oauth/import-callback` | `{"session_id":...,"callback_url":...}` |
| `GET /oauth/callback` | portal 回调入口（本机浏览器直连时用） |

```bash
export CA2A_API_KEY=...
curl -s http://127.0.0.1:8787/v1/models -H "Authorization: Bearer $CA2A_API_KEY"
curl -s -X POST http://127.0.0.1:8787/admin/api/checkin \
  -H "Authorization: Bearer $CA2A_API_KEY" -H "Content-Type: application/json" -d '{}'
curl -s -X POST http://127.0.0.1:8787/admin/api/keepalive \
  -H "Authorization: Bearer $CA2A_API_KEY" -H "Content-Type: application/json" -d '{}'
```

---

## 7. 账号池与错误处理（为什么这么设计）

| 情况 | 处理 | 理由 |
|---|---|---|
| 5xx | 换下一个账号，并冷却该账号 `err_cooldown`（默认 300s） | 上游确实坏了，等一会儿再试 |
| 429 / 并发会话 / TPM 限流 | **不冷却**，原地等待 `queue.retry_seconds` 后重试（默认 10s × 30 次） | 限流是瞬时的，冷却反而把可用账号藏起来 |
| 风控（`3012` / `405` unusual activity） | 指数退避 `900s·2ⁿ⁻¹`（上限 24h）；累计 4 次 → 禁用（人工恢复） | 调用太频繁的信号，退避越久越安全；屡教不改直接停用 |
| 401 / `refresh_token` 终态失效 | 标记 `invalid` 并禁用，需要重新登录 | 与瞬时网络错区别对待，避免无意义重试 |
| `InferHub.001001005` 参数错误 | 返回 400，**不轮换、不冷却** | 客户端请求形状问题，换账号结果相同 |
| `not registered` / `benefit not found` | 记为「该模型对这个账号不可用」，返回 404，**不给账号记错误** | 账号能力问题，不是账号故障（曾因此打停整池） |
| 满并发 | 跳过该账号，**不计入重试轮数** | 上游单账号并发会话上限实测只有 3 个且释放很慢 |
| 签到接口故障 | **只记录签到结果**，绝不计入推理错误 / 冷却 / 禁用 | 一次活动接口抖动不该影响账号能否推理 |

**模型可用性探测**：上游的模型发现会列出账号根本没注册的模型，因此服务会用一条最小
请求真实探测并缓存结论（**30 分钟**）。`/v1/models` 只列真实可用的模型；真实请求失败
也会被学习。探测会避让真实流量（账号空闲 10 分钟以上才探），避免抢占上游并发槽位。

---

## 8. 部署

### 8.1 systemd

```bash
sudo mkdir -p /opt/codearts2api
sudo cp -r app server.py benefit.py login.py requirements.txt config.example.json /opt/codearts2api/
cd /opt/codearts2api && sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.json config.json
sudo tee /opt/codearts2api/.env >/dev/null <<'EOF'
CA2A_API_KEY=换成你自己的随机密钥
# 二选一：永久 AK/SK，或先跑一次 `python login.py` 生成 auths/ 下的 OAuth 凭证
CODEARTS_AK=...
CODEARTS_SK=...
EOF
sudo chmod 600 /opt/codearts2api/.env

# 写 unit（本仓库不含部署脚本，直接贴）
sudo tee /etc/systemd/system/codearts2api.service >/dev/null <<'EOF'
[Unit]
Description=CodeArts2API (OpenAI-compatible proxy for Huawei Cloud CodeArts)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/codearts2api
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/codearts2api/.env
ExecStart=/opt/codearts2api/.venv/bin/python /opt/codearts2api/server.py
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload && sudo systemctl enable --now codearts2api
journalctl -u codearts2api -f
```

> ⚠️ **改完代码重新部署时用 `systemctl restart`，不要用 `enable --now`。**
> `enable --now` 的语义是「开机自启 + 没在跑才启动」——服务**已经在跑**时它什么都
> 不做，于是文件全换成新的了、进程还是旧代码，而日志看起来完全正常。

### 8.2 Docker

```bash
cp .env.example .env          # 填 CA2A_API_KEY（+ 可选 AK/SK）
mkdir -p auths data
docker compose up -d --build
docker compose exec codearts2api python login.py --print-only   # 可选：OAuth 登录
```

容器内监听 `0.0.0.0:8787`，但 compose 只把它发布到宿主机 `127.0.0.1:8787`。
需要被其它主机访问时改端口映射，**并确认 `CA2A_API_KEY` 足够强**，最好再加一层反代鉴权。

### 8.3 定时签到（可选）

服务自身已经会自动签到（启动时立刻一次，之后每 `poll_minutes`）。
如果你更习惯用系统计划任务，`benefit.py` 的 CLI 用法与基线完全一致：

```bash
python benefit.py status    # 只查询
python benefit.py claim     # 只签到
python benefit.py models    # 免费模型列表
```

```cmd
schtasks /create /tn CodeArtsBenefit /tr "python C:\...\benefit.py claim" /sc daily /st 09:05
```

---

## 9. 自检与踩坑记录

本仓库不含测试目录，下面是几条不依赖测试框架的冒烟检查。

```bash
python -m compileall .                                  # 1. 语法检查

# 2. 起服务后（默认 127.0.0.1:8787）
curl -s -o /dev/null -w '存活探针  %{http_code}\n' http://127.0.0.1:8787/healthz
curl -s -o /dev/null -w '面板      %{http_code}\n' http://127.0.0.1:8787/
curl -s -o /dev/null -w '无key鉴权 %{http_code}\n' http://127.0.0.1:8787/v1/models
curl -s -H "Authorization: Bearer $CA2A_API_KEY" \
     http://127.0.0.1:8787/v1/models | head -c 300       # 3. 模型列表

# 4. 真发一次对话
curl -s -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer $CA2A_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"GLM-5.2","messages":[{"role":"user","content":"你好"}]}'
```

预期：`/healthz` 与面板 `200`、无 key 的 `/v1/models` 是 `401`。
（`/healthz` 不鉴权，供容器健康检查与反代存活探测用，Dockerfile 的
`HEALTHCHECK` 就是打它。）

管理接口（都带同一个 key）：

| 接口 | 作用 |
|---|---|
| `GET  /admin/api/overview` | 账号池 / 模型 / 调度状态总览 |
| `POST /admin/api/credits` | 校验账号（真实上游请求，顺带刷额度） |
| `POST /admin/api/checkin` | 签到（积分活动 + 免费额度） |
| `POST /admin/api/quota` | **只刷可用额度**（面板「刷新」按钮走这个） |
| `POST /admin/api/keepalive` | 只续期凭证 |
| `POST /admin/api/oauth/start` → `poll` / `import-callback` | WebUI 用的 OAuth 会话 |

### 改代码前值得先看的几个坑（都实测过）

- **`role: "developer"`** —— DSH 0.1.7 起用它承载系统提示词，本上游不认：
  `InferHub.001001044.400 Message role cannot empty`。已在
  `upstream.normalize_message_roles()` 里**改名成 `system` 而不是丢弃** ——
  丢弃会静默丢掉整份系统提示词，模型行为变差且完全没有报错。
- **`max_tokens` 硬上限 65536** —— 超过**不报错**，而是返回 `HTTP 200` + **内容全空**
  （二分实测：65536 正常、66048 起 content 与 reasoning 全为空，GLM 与 deepseek
  系列阈值一致）。而上游 `/v1/models` 自报的 `max_tokens`（384000 / 393216 /
  131072）**全都超过这个上限**，照抄它就会让客户端每次都收到「成功但什么都没有」。
  `upstream.clamp_max_tokens()` 按实测上限夹取，可用 `CA2A_MAX_OUTPUT_TOKENS`
  覆盖（设 0 = 关闭夹取）。
- **InferHub 4xx 是客户端错误，不能算账号故障** —— 否则一个客户端发错字段就能把
  整池账号冷却、网关回 503。另外**判定顺序上风控优先于参数错误**：该上游的 405
  是风控（unusual activity），被「参数错误」分支吞掉比误冷却更危险。
- **DPoP 签名必须用 `Prehashed`** —— `ec.ECDSA(hashes.SHA256())` 的语义是「先对
  传入数据做 SHA256 再签名」，传**预计算摘要**会变成签 `SHA256(SHA256(input))`，
  服务端一律拒绝，而报错伪装成 `InvalidDPoPHeader` / 「密钥不对」。
  这个 bug **只做「自己签自己验」的 round-trip 抓不到**（两边同错照样通过），
  必须像服务端那样用 `utils.Prehashed(hashes.SHA256())` 独立验签。
- **原子写要用唯一临时名** —— 固定 `path + ".tmp"` 在并发保存时会互相踩，
  Windows 上表现为 `os.replace` 的 `PermissionError [WinError 32]`
  （POSIX 下则是静默互相覆盖内容，更隐蔽）。见 `app/atomic.py`。
- **`max_tokens` 别给太小** —— 本上游的模型（GLM-5.x 等）是恒思考模型，
  `max_tokens=300` 这种小值会被思考全吃光、正文为空。建议 ≥ 2048。

---

## 10. 代码结构

```text
codearts2api-py/
├── server.py                 # 入口（python server.py）
├── login.py                  # OAuth 登录 CLI（--print-only）
├── benefit.py                # 原 CLI：status / claim / models（用法不变）
├── app/
│   ├── config.py             # env + config.json
│   ├── const.py              # 上游端点常量（对齐 Go const.go）
│   ├── signing.py            # SDK-HMAC-SHA256（通用签名器 + 聊天双通道）
│   ├── dpop.py               # DPoP ES256 密钥与证明
│   ├── oauth.py              # OAuth2 PKCE + DPoP（三种登录方式）
│   ├── store.py              # 凭证 / 池状态 / 会话映射持久化
│   ├── atomic.py             # 原子写（唯一临时名 + 重试）
│   ├── pool.py               # 账号池：轮转/冷却/风控/并发/续期/额度
│   ├── checkin.py            # 签到：积分活动状态机 + opengw 免费额度 + 额度查询
│   ├── models.py             # 模型发现 + 可用性探测 + 限时福利领取
│   ├── upstream.py           # 聊天客户端 + SSE 聚合/流式 + 错误分类
│   ├── scheduler.py          # 看门狗：续期 + 保活 + 自动签到 + 探测
│   ├── server.py             # Flask：OpenAI 路由 + 管理 API + WebUI
│   └── web/panel.html        # WebUI（单文件，内嵌 CSS/JS，零 CDN）
├── config.example.json
├── Dockerfile / docker-compose.yml
└── README.md
```

---

## 11. 已知限制

- **`max_tokens` 上限 65536 是实测的**，但上游随时可能改；改了就调整
  `CA2A_MAX_OUTPUT_TOKENS`（或改 `upstream.MAX_OUTPUT_TOKENS`）。
  上游自报的 `max_tokens` 字段**不可信**（见第 9 节）。
- 上游没有「积分余额」接口（`/v1/ops/balance` 等一律 404）。能查到的「还剩多少」
  只有 opengw 的 `/api/v1/user/tokens/balance`，面板上显示的可用额度来自它，
  单位是 token 而不是活动积分。
- WebUI 授权会话存在内存里，**服务重启会让未完成的授权失效**（需重新发起）。
- 永久 AK/SK 账号的 `/admin/api/accounts/delete` 会被拒绝（它没有凭证文件），
  请从 `.env` 移除后重启。
- 流式 usage 在上游未返回时是**按字符数估算**的，不是精确 token 计数。
- `max_concurrent` 默认 2：上游单账号并发会话上限实测只有 3 个且槽位释放很慢
  （>15s），调高容易撞 `TM.00001041`。
- 恒思考模型（GLM-5.x 等）**关不掉思考**：实测 `reasoning_effort` 无效，
  只有 `thinking:{"type":"disabled"}` / `enable_thinking:false` 才生效；
  另外上游按平台拒绝非法 role，客户端别自己发明角色名。
- 使用 Flask 自带开发服务器（与基线一致）。生产环境建议前置 nginx 或改用
  waitress/gunicorn，注意 SSE 需要关闭响应缓冲（`X-Accel-Buffering: no` 已设置）。

---

## 12. 说明

- 代理只做签名与转发，不修改模型行为。
- 每次调用会消耗华为云 CodeArts 套餐额度（token）。
- 默认只绑定 `127.0.0.1`，不会对外网开放。
- 本项目及本项目作者与华为云平台及其附属公司无任何关系，且华为云平台中并未对本项目中
  调用方式明确提出允许，若造成封号/限流等任何因使用本工具造成的情况与项目作者无关。
  如果您使用该项目，则默认视为同意本 Readme 中所有说明。

# 社区

感谢 [LinuxDO](https://linux.do/) 对本项目进行推广。

## License

MIT
