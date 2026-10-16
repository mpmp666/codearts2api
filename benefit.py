# -*- coding: utf-8 -*-
"""CodeArts 免费额度签到/查询工具（CLI 用法保持不变）。

对应 IDE 里的免费模型福利机制（opengw.developer.huaweicloud.com，抓包确认）：

    GET  /api/v1/benefit/claim        签到状态（返回上次签到时间）
    POST /api/v1/benefit/claim        签到领取当日免费额度（空 body）
    GET  /api/v1/user/tokens/balance  查询免费额度余额
    GET  /api/v1/gateway/config       免费模型列表

用法（与原版完全一致）：

    python benefit.py            # 签到 + 查询余额
    python benefit.py status     # 只查询，不签到
    python benefit.py claim      # 只签到
    python benefit.py models     # 查询免费模型列表

增强：凭证不再只认 `.env` 的永久 AK/SK —— `auths/` 下的 OAuth 凭证同样可用（多账号时
逐个执行）。所有网络/签名逻辑复用 `app.signing` / `app.checkin`，避免与主服务两套实现
各自漂移（这正是 Go 版踩过的坑：签到逻辑在两个地方各写一份）。

配合 Windows 计划任务可实现每日自动签到，例如每天 09:05 执行：
    schtasks /create /tn CodeArtsBenefit /tr "python C:\\...\\benefit.py claim" /sc daily /st 09:05
"""
import os
import sys

from app.checkin import BenefitClient, format_time_ms
from app.config import PROJECT_ROOT, ConfigError, load_config
from app.store import load_accounts

# 兼容基线：外部脚本可能 `from benefit import signed_request`
from app.signing import sign_request as signed_request  # noqa: F401


def _load_accounts():
    """读取可用账号（永久 AK/SK + OAuth）。没有任何凭证时按基线文案报错退出。

    注意 require_api_key=False：这是本地 CLI，不发 HTTP、不对外暴露，而且
    「先用 AK/SK 签到、之后再配 API Key」是正常顺序。API Key 是给服务用的。
    """
    try:
        cfg = load_config(require_credentials=False, require_api_key=False)
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}")
    accounts = load_accounts(cfg)
    if not accounts:
        raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 .env 中配置后重试")
    return accounts


def _client(cred) -> BenefitClient:
    return BenefitClient(cred.sign_credential())


def claim_status(cred=None) -> dict:
    """查询签到状态。create_time 即最近一次签到时间（毫秒时间戳）。"""
    return _client(cred or _load_accounts()[0]).status()


def claim(cred=None) -> dict:
    """签到领取当日免费额度。"""
    return _client(cred or _load_accounts()[0]).claim()


def balance(cred=None) -> dict:
    """查询免费额度余额。"""
    return _client(cred or _load_accounts()[0]).balance()


def models(cred=None) -> list:
    """查询免费模型列表。"""
    return _client(cred or _load_accounts()[0]).models()


def _fmt_time(ms) -> str:
    return format_time_ms(ms)


def show_balance(cred=None) -> None:
    info = balance(cred)
    total_quota = info.get("total_quota") or 0
    remain = info.get("total_balance") or 0
    used = info.get("used_amount") or 0
    percent = (remain / total_quota * 100) if total_quota else 0
    print(f"免费额度余额: {remain:,} / {total_quota:,}（已用 {used:,}，剩余 {percent:.1f}%）")


def show_status(cred=None) -> None:
    info = claim_status(cred)
    print(f"最近签到时间: {_fmt_time(info.get('create_time'))}")


def do_claim(cred=None) -> None:
    info = claim(cred)
    print(f"签到成功，最近签到时间: {_fmt_time(info.get('create_time'))}")


def show_models(cred=None) -> None:
    for model in models(cred):
        ctx = model.get("context_window")
        out = model.get("max_tokens")
        if ctx:
            print(f"  {model.get('model_id')}  (上下文 {ctx:,} / 最大输出 {out:,})")
        else:
            print(f"  {model.get('model_id')}")


def main(argv: list) -> int:
    action = argv[1] if len(argv) > 1 else "default"
    try:
        accounts = _load_accounts()
        for index, cred in enumerate(accounts):
            if len(accounts) > 1:
                print(f"[{index + 1}/{len(accounts)}] 账号 {cred.label}（{cred.mode}）")
            if action == "status":
                show_status(cred)
            elif action == "claim":
                do_claim(cred)
                show_balance(cred)
            elif action == "models":
                if index == 0:
                    print("免费模型列表:")
                show_models(cred)
            else:
                # 默认：状态 + 签到 + 余额
                show_status(cred)
                do_claim(cred)
                show_balance(cred)
    except SystemExit:
        raise
    except Exception as error:  # 基线把所有失败收敛成一行 "失败: ..."，保持这个行为
        print(f"失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    # 保证从任意工作目录执行都能 import app.*
    sys.path.insert(0, PROJECT_ROOT)
    sys.exit(main(sys.argv))
