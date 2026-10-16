# -*- coding: utf-8 -*-
"""配置：`.env`（密钥）+ `config.json`（行为）两层。

为什么分两层：`.env` 是基线就有的习惯（AK/SK 与端口），已经写进很多人的部署脚本和
Windows 计划任务；`config.json` 承载增强版新增的行为开关（冷却时长、探测周期、
自动签到…）。密钥永远只从 `.env` / 环境变量读，避免把 AK/SK 提交进 git。

配置项命名对齐 Go 版（`CA2A_*` / config.json 同名字段），这样从 Go 版迁过来的人
不用重新学一套；同时保留基线原有的 `CODEARTS_*` 变量，老部署不改也能跑。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigError(RuntimeError):
    """配置错误。启动阶段抛出，由入口打印明确原因后退出（而不是裸奔启动）。"""


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_duration(value, default: float) -> float:
    """把 "300s" / "5m" / "1h" / 数字（秒）解析成秒。

    支持 Go 风格后缀是因为 config.json 的时长字段直接从 Go 版抄过来更省事，
    手写 `"err_cooldown": "10m"` 比 `600` 可读得多。
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(ms|s|m|h|d)?", text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2) or "s"
    factor = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
    return number * factor


def strip_json_comments(raw: str) -> str:
    """剥离 config.json 里的 // 与 /* */ 注释。

    config.example.json 带注释（字段说明只写在那一份里），README 又让人直接
    `cp config.example.json config.json`；标准 json 模块不接受注释，所以先剥一层。
    字符串内部的 // 与 /* 不动（oauth_callback_host 这类值本身就是 URL）。
    """
    out = []
    in_string = False
    escaped = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            while i < len(raw) and raw[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "*":
            i += 2
            while i + 1 < len(raw) and not (raw[i] == "*" and raw[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass
class CooldownConfig:
    """错误冷却与限流等待参数。"""

    err_threshold: int = 3          # 连续错误多少次后冷却
    err_cooldown: float = 300.0     # 5xx 重试耗尽后的冷却时长（秒）
    soft_rate: float = 60.0         # 软冷却（备用，默认不用于 429）


@dataclass
class RiskConfig:
    """风控（unusual activity）退避参数。

    3012/405 是华为云侧的异常活动标记：它通常来自短时间高频调用，等待越久越安全，
    所以用指数退避（base * 2^(n-1)）而不是固定冷却，并设 24h 上限防止退避到无限。
    """

    base_seconds: float = 900.0     # 首次风控冷却 15 分钟
    max_seconds: float = 86400.0    # 上限 24 小时
    max_strikes: int = 4            # 累计多少次升级为禁用（人工恢复）


@dataclass
class WatchConfig:
    """看门狗（调度器）参数。"""

    enabled: bool = True
    poll_minutes: int = 30            # 轮询间隔
    refresh_skew_minutes: int = 30    # 到期前多久主动刷新
    keepalive_interval_minutes: int = 15   # 保活轻量请求间隔
    keepalive_window_minutes: int = 10     # 多久没活动算「需要保活」


@dataclass
class QueueConfig:
    """上游排队/TPM 限流时的原地等待重试参数。"""

    retry_seconds: int = 10
    max_attempts: int = 30            # 10s × 30 ≈ 5 分钟


@dataclass
class AvailabilityConfig:
    """模型可用性探测参数。"""

    ttl_minutes: int = 30             # 探测结论缓存时长（SPEC 要求 30 分钟）
    probe_interval_minutes: int = 15  # 后台批量探测间隔
    probe_idle_before_minutes: int = 10   # 账号空闲多久才允许探测
    probe_gap_seconds: int = 20       # 两次探测之间留出上游会话槽位释放时间
    probe_timeout_seconds: int = 45


@dataclass
class Config:
    """运行时配置总集。"""

    # --- 监听 ---
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787

    # --- 鉴权 ---
    api_key: str = ""

    # --- 凭证 ---
    mode: str = "auto"               # auto | apikey | oauth
    ak: str = ""
    sk: str = ""
    auth_dir: str = ""
    state_file: str = ""
    chats_file: str = ""
    models_cache: str = ""

    # --- 模型 ---
    default_model: str = "glm-5.2"
    max_rotate: int = 3              # 单次请求最多轮换几个账号
    max_concurrent: int = 2          # 单账号并发上限（0=不限）

    # --- 行为开关 ---
    benefit_auto_claim: bool = True   # 模型发现时领取限时福利（不领取则福利模型必然 404）
    credits_auto_claim: bool = True   # 调度器自动签到

    # --- OAuth ---
    login_client_id: str = ""
    oauth_callback_host: str = ""
    oauth_session_ttl: float = 900.0

    # --- 上游 ---
    upstream_timeout: int = 120
    queue: QueueConfig = field(default_factory=QueueConfig)
    cooldown: CooldownConfig = field(default_factory=CooldownConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    availability: AvailabilityConfig = field(default_factory=AvailabilityConfig)

    # --- 来源记录（WebUI 展示用） ---
    config_path: str = ""

    # require_accounts=True 时，启动必须已有账号（凭证文件或 .env AK/SK）。
    #
    # 为什么默认 False：SPEC 要求「无凭证时启动明确报错」，但同一份 SPEC 又要求
    # 「有 CA2A_API_KEY 但无账号时 /v1/models 返回空列表且不崩」—— 而 WebUI 的
    # 「＋ 授权登录」正是**获取**凭证的正常途径。若默认拒绝启动，全新部署就永远
    # 走不到登录那一步。因此这里的规则是：
    #   * 没有 api_key              → 一律拒绝启动（防裸奔，这是硬要求）
    #   * mode=apikey 且没有 AK/SK  → 一律拒绝启动（该模式没有任何补凭证的途径）
    #   * mode=auto/oauth 且没有账号 → 允许启动，但打印醒目警告提示去授权登录
    # 需要在无人值守部署里强制「必须有账号」时，把它设成 true 即可。
    require_accounts: bool = False

    def pool_enabled_accounts_hint(self) -> str:
        return "oauth" if self.mode == "oauth" else ("apikey" if self.mode == "apikey" else "auto")


def _apply_json(cfg: Config, data: dict) -> None:
    """把 config.json 的字段灌进 Config。未知字段忽略（向前兼容）。"""
    if not isinstance(data, dict):
        raise ConfigError("config.json 顶层必须是对象")

    def pick(key, current):
        return data[key] if key in data and data[key] is not None else current

    cfg.listen_host = str(pick("listen_host", cfg.listen_host))
    cfg.listen_port = int(pick("listen_port", cfg.listen_port))
    cfg.api_key = str(pick("api_key", cfg.api_key))
    cfg.mode = str(pick("mode", cfg.mode)).lower()
    cfg.auth_dir = str(pick("auth_dir", cfg.auth_dir))
    cfg.state_file = str(pick("state_file", cfg.state_file))
    cfg.chats_file = str(pick("chats_file", cfg.chats_file))
    cfg.models_cache = str(pick("models_cache", cfg.models_cache))
    cfg.default_model = str(pick("default_model", cfg.default_model))
    cfg.max_rotate = int(pick("max_rotate", cfg.max_rotate))
    cfg.max_concurrent = int(pick("max_concurrent", cfg.max_concurrent))
    cfg.benefit_auto_claim = bool(pick("benefit_auto_claim", cfg.benefit_auto_claim))
    cfg.credits_auto_claim = bool(pick("credits_auto_claim", cfg.credits_auto_claim))
    cfg.login_client_id = str(pick("login_client_id", cfg.login_client_id))
    cfg.oauth_callback_host = str(pick("oauth_callback_host", cfg.oauth_callback_host))
    cfg.oauth_session_ttl = parse_duration(pick("oauth_session_ttl", None), cfg.oauth_session_ttl)
    cfg.require_accounts = bool(pick("require_accounts", cfg.require_accounts))

    upstream = data.get("upstream") or {}
    if isinstance(upstream, dict):
        cfg.upstream_timeout = int(upstream.get("timeout_seconds", cfg.upstream_timeout))

    cooldown = data.get("cooldown") or {}
    if isinstance(cooldown, dict):
        cfg.cooldown.err_threshold = int(cooldown.get("err_threshold", cfg.cooldown.err_threshold))
        cfg.cooldown.err_cooldown = parse_duration(cooldown.get("err_cooldown"), cfg.cooldown.err_cooldown)
        cfg.cooldown.soft_rate = parse_duration(cooldown.get("soft_rate"), cfg.cooldown.soft_rate)

    risk = data.get("risk") or {}
    if isinstance(risk, dict):
        cfg.risk.base_seconds = parse_duration(risk.get("base_seconds"), cfg.risk.base_seconds)
        cfg.risk.max_seconds = parse_duration(risk.get("max_seconds"), cfg.risk.max_seconds)
        cfg.risk.max_strikes = int(risk.get("max_strikes", cfg.risk.max_strikes))

    watch = data.get("watch") or {}
    if isinstance(watch, dict):
        cfg.watch.enabled = bool(watch.get("enabled", cfg.watch.enabled))
        cfg.watch.poll_minutes = int(watch.get("poll_minutes", cfg.watch.poll_minutes))
        cfg.watch.refresh_skew_minutes = int(watch.get("refresh_skew_minutes", cfg.watch.refresh_skew_minutes))
        cfg.watch.keepalive_interval_minutes = int(
            watch.get("keepalive_interval_minutes", cfg.watch.keepalive_interval_minutes)
        )
        # 兼容 Go 版配置名 keepalive_window: "10m"
        cfg.watch.keepalive_window_minutes = int(
            parse_duration(watch.get("keepalive_window"), cfg.watch.keepalive_window_minutes * 60) / 60
        )

    queue = data.get("queue") or {}
    if isinstance(queue, dict):
        cfg.queue.retry_seconds = int(queue.get("retry_seconds", cfg.queue.retry_seconds))
        cfg.queue.max_attempts = int(queue.get("max_attempts", cfg.queue.max_attempts))

    availability = data.get("availability") or {}
    if isinstance(availability, dict):
        cfg.availability.ttl_minutes = int(availability.get("ttl_minutes", cfg.availability.ttl_minutes))
        cfg.availability.probe_interval_minutes = int(
            availability.get("probe_interval_minutes", cfg.availability.probe_interval_minutes)
        )
        cfg.availability.probe_idle_before_minutes = int(
            availability.get("probe_idle_before_minutes", cfg.availability.probe_idle_before_minutes)
        )
        cfg.availability.probe_gap_seconds = int(
            availability.get("probe_gap_seconds", cfg.availability.probe_gap_seconds)
        )
        cfg.availability.probe_timeout_seconds = int(
            availability.get("probe_timeout_seconds", cfg.availability.probe_timeout_seconds)
        )


def _apply_env(cfg: Config) -> None:
    """环境变量覆盖。CA2A_* 优先，其次基线原有的 CODEARTS_*。"""
    # 密钥：AK/SK 只从环境读，绝不写进 config.json
    cfg.ak = _env("CODEARTS_AK", cfg.ak)
    cfg.sk = _env("CODEARTS_SK", cfg.sk)

    if _env("CA2A_API_KEY"):
        cfg.api_key = _env("CA2A_API_KEY")
    if _env("CA2A_LISTEN_HOST"):
        cfg.listen_host = _env("CA2A_LISTEN_HOST")
    if _env("CODEARTS_PROXY_HOST"):
        # 基线变量：老部署改端口只改这一个，必须继续生效
        cfg.listen_host = _env("CODEARTS_PROXY_HOST")
    if _env("CA2A_LISTEN_PORT"):
        cfg.listen_port = _env_int("CA2A_LISTEN_PORT", cfg.listen_port)
    if _env("CODEARTS_PROXY_PORT"):
        cfg.listen_port = _env_int("CODEARTS_PROXY_PORT", cfg.listen_port)

    if _env("CA2A_MODE"):
        cfg.mode = _env("CA2A_MODE").lower()
    if _env("CA2A_AUTH_DIR"):
        cfg.auth_dir = _env("CA2A_AUTH_DIR")
    if _env("CA2A_STATE_FILE"):
        cfg.state_file = _env("CA2A_STATE_FILE")
    if _env("CA2A_CHATS_FILE"):
        # 单独可覆盖是必要的：测试与验收脚本必须把会话映射写到临时目录，
        # 否则会污染仓库里的 data/chats.json（并让并行测试互相踩）。
        cfg.chats_file = _env("CA2A_CHATS_FILE")
    if _env("CA2A_MODELS_CACHE"):
        cfg.models_cache = _env("CA2A_MODELS_CACHE")
    if _env("CA2A_DEFAULT_MODEL"):
        cfg.default_model = _env("CA2A_DEFAULT_MODEL")
    if _env("CA2A_MAX_ROTATE"):
        cfg.max_rotate = _env_int("CA2A_MAX_ROTATE", cfg.max_rotate)
    if _env("CA2A_MAX_CONCURRENT"):
        cfg.max_concurrent = _env_int("CA2A_MAX_CONCURRENT", cfg.max_concurrent)
    if _env("CA2A_LOGIN_CLIENT_ID"):
        cfg.login_client_id = _env("CA2A_LOGIN_CLIENT_ID")
    if _env("CA2A_OAUTH_CALLBACK_HOST"):
        cfg.oauth_callback_host = _env("CA2A_OAUTH_CALLBACK_HOST")
    if _env("CA2A_WATCH_ENABLED"):
        cfg.watch.enabled = _env_bool("CA2A_WATCH_ENABLED", cfg.watch.enabled)
    if _env("CA2A_WATCH_POLL_MINUTES"):
        cfg.watch.poll_minutes = _env_int("CA2A_WATCH_POLL_MINUTES", cfg.watch.poll_minutes)
    if _env("CA2A_WATCH_REFRESH_SKEW"):
        cfg.watch.refresh_skew_minutes = _env_int("CA2A_WATCH_REFRESH_SKEW", cfg.watch.refresh_skew_minutes)
    if _env("CA2A_WATCH_KEEPALIVE_INTERVAL"):
        cfg.watch.keepalive_interval_minutes = _env_int(
            "CA2A_WATCH_KEEPALIVE_INTERVAL", cfg.watch.keepalive_interval_minutes
        )
    if _env("CA2A_KEEPALIVE_WINDOW"):
        cfg.watch.keepalive_window_minutes = int(
            parse_duration(_env("CA2A_KEEPALIVE_WINDOW"), cfg.watch.keepalive_window_minutes * 60) / 60
        )
    if _env("CA2A_QUEUE_RETRY_SECONDS"):
        cfg.queue.retry_seconds = _env_int("CA2A_QUEUE_RETRY_SECONDS", cfg.queue.retry_seconds)
    if _env("CA2A_QUEUE_MAX_ATTEMPTS"):
        cfg.queue.max_attempts = _env_int("CA2A_QUEUE_MAX_ATTEMPTS", cfg.queue.max_attempts)
    if _env("CA2A_BENEFIT_AUTO_CLAIM"):
        cfg.benefit_auto_claim = _env_bool("CA2A_BENEFIT_AUTO_CLAIM", cfg.benefit_auto_claim)
    if _env("CA2A_CREDITS_AUTO_CLAIM"):
        cfg.credits_auto_claim = _env_bool("CA2A_CREDITS_AUTO_CLAIM", cfg.credits_auto_claim)
    if _env("CA2A_UPSTREAM_TIMEOUT_SECONDS"):
        cfg.upstream_timeout = _env_int("CA2A_UPSTREAM_TIMEOUT_SECONDS", cfg.upstream_timeout)
    if _env("CA2A_AVAILABILITY_TTL_MINUTES"):
        cfg.availability.ttl_minutes = _env_int(
            "CA2A_AVAILABILITY_TTL_MINUTES", cfg.availability.ttl_minutes
        )
    if _env("CA2A_REQUIRE_ACCOUNT"):
        cfg.require_accounts = _env_bool("CA2A_REQUIRE_ACCOUNT", cfg.require_accounts)


def _normalize(cfg: Config) -> None:
    """补默认值 + 把相对路径锚定到项目根目录。

    为什么锚定：systemd / docker 的工作目录未必是项目目录，相对路径会飘；基线把
    models-cache.json 放在脚本同级目录，这里沿用同样的语义。
    """
    def anchor(path: str) -> str:
        if not path:
            return path
        return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)

    cfg.auth_dir = anchor(cfg.auth_dir or "./auths")
    cfg.state_file = anchor(cfg.state_file or "./data/state.json")
    cfg.chats_file = anchor(cfg.chats_file or "./data/chats.json")
    cfg.models_cache = anchor(cfg.models_cache or "./models-cache.json")

    if cfg.mode not in ("auto", "apikey", "oauth"):
        raise ConfigError(f'mode 只能是 auto / apikey / oauth，当前为 "{cfg.mode}"')
    if cfg.max_rotate <= 0:
        cfg.max_rotate = 3
    if cfg.max_concurrent < 0:
        cfg.max_concurrent = 0
    if cfg.cooldown.err_threshold <= 0:
        cfg.cooldown.err_threshold = 3
    if cfg.cooldown.err_cooldown <= 0:
        cfg.cooldown.err_cooldown = 300.0
    if cfg.watch.poll_minutes <= 0:
        cfg.watch.poll_minutes = 30
    if cfg.watch.refresh_skew_minutes <= 0:
        cfg.watch.refresh_skew_minutes = 30
    if cfg.watch.keepalive_interval_minutes <= 0:
        cfg.watch.keepalive_interval_minutes = 15
    if cfg.risk.max_strikes <= 0:
        cfg.risk.max_strikes = 4
    if cfg.risk.base_seconds <= 0:
        cfg.risk.base_seconds = 900.0
    if cfg.risk.max_seconds <= 0:
        cfg.risk.max_seconds = 86400.0
    if cfg.upstream_timeout <= 0:
        cfg.upstream_timeout = 120
    if cfg.queue.max_attempts <= 0:
        cfg.queue.max_attempts = 30
    if cfg.queue.retry_seconds <= 0:
        cfg.queue.retry_seconds = 10
    if not cfg.default_model:
        cfg.default_model = "glm-5.2"


def load_config(
    config_path: str = "",
    *,
    require_credentials: bool = True,
    require_api_key: bool = True,
) -> Config:
    """加载配置。

    require_api_key=True 时，没有 api_key 直接抛 ConfigError。这是 SPEC 的硬要求：
    没有 API Key 等于把 `/v1/chat/completions` 暴露给任何人，因此 HTTP 服务必须
    「拒绝裸奔启动」。

    但**本地 CLI 工具**（`benefit.py` / `login.py`）不能受这条约束：它们不发 HTTP、
    也不对外暴露任何东西，而且「先登录拿凭证、再配 API Key」是完全正常的顺序。
    所以那两个入口传 require_api_key=False。

    require_credentials=True 时，没有任何可用凭证就抛 ConfigError（规则见
    Config.require_accounts）。
    """
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

    cfg = Config()
    path = config_path or os.getenv("CA2A_CONFIG", "").strip()
    if not path:
        candidate = os.path.join(PROJECT_ROOT, "config.json")
        path = candidate if os.path.exists(candidate) else ""
    if path:
        if not os.path.isabs(path):
            path = os.path.join(PROJECT_ROOT, path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as error:
            raise ConfigError(f"读取配置文件失败: {error}") from error
        try:
            data = json.loads(strip_json_comments(raw))
        except ValueError as error:
            raise ConfigError(f"解析 {path} 失败: {error}") from error
        _apply_json(cfg, data)
        cfg.config_path = path

    _apply_env(cfg)
    _normalize(cfg)

    if require_api_key and not cfg.api_key:
        raise ConfigError(
            "api_key 未设置：请在 config.json 写 api_key，或设置环境变量 CA2A_API_KEY "
            "后再启动（生成示例：python -c \"import secrets;print(secrets.token_hex(24))\"）。"
            "没有 API Key 等于把 /v1/chat/completions 暴露给任何人，因此拒绝启动。"
        )

    if require_credentials:
        reason = missing_credential_reason(cfg)
        if reason:
            raise ConfigError(reason)
    return cfg


def has_any_credential(cfg: Config) -> bool:
    """判断是否存在至少一份可用凭证。

    只看「有没有」，不在这里校验有效性：无效凭证由账号池标记 invalid，启动不该因此失败
    （否则一个过期账号会让整个服务起不来，运维还得手工删文件）。
    """
    if cfg.mode in ("auto", "apikey") and cfg.ak and cfg.sk:
        return True
    if cfg.mode in ("auto", "oauth") and os.path.isdir(cfg.auth_dir):
        for name in os.listdir(cfg.auth_dir):
            if name.startswith("codearts-") and name.endswith(".json"):
                return True
    return False


def missing_credential_reason(cfg: Config) -> str:
    """返回「必须拒绝启动」的原因；不需要拒绝时返回空串。

    规则见 Config.require_accounts 的注释：永久 AK/SK 模式没有任何补凭证途径，
    因此缺凭证必须报错；OAuth/auto 模式可以通过 `python login.py` 或 WebUI 的
    「＋ 授权登录」补上，允许先启动。
    """
    if has_any_credential(cfg):
        return ""
    hint = (
        "请配置 .env 的 CODEARTS_AK/CODEARTS_SK（永久 AK/SK 模式），"
        "或先运行 `python login.py` 完成 OAuth 授权把凭证写入 auths/，"
        "或启动后在 WebUI 点「＋ 授权登录」。"
    )
    if cfg.require_accounts:
        return f"require_accounts=true 且没有任何可用凭证：{hint}（auth_dir={cfg.auth_dir}）"
    if cfg.mode == "apikey":
        return (
            f"mode=apikey 但缺少 CODEARTS_AK/CODEARTS_SK，该模式没有其它补凭证的途径：{hint}"
        )
    return ""
