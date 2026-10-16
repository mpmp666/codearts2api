# -*- coding: utf-8 -*-
"""CodeArts OAuth 登录工具（对应 Go 版的 `./login.sh`）。

三种用法：

    python login.py                  # 本机有浏览器：起本地回调 + 自动开浏览器
    python login.py --print-only     # 服务器无浏览器：只打印授权链接，纯 ticket 轮询
    python login.py --client-id xxx  # 覆盖 OAuth client_id（默认 codearts-agent）

凭证落盘 `auths/codearts-{user_id}.json`，字段与 Go 版 `internal/auth` 对齐，
因此两边的 auths 目录可以互相搬运。

**为什么必须把 DPoP 私钥一起落盘**：refresh_token 与签发时的 DPoP 公钥绑定，
换密钥去刷新会被 STS 拒（`invalid refresh token: InvalidDPoPHeader`）。
"""
from __future__ import annotations

import argparse
import os
import sys

# Windows 控制台默认 GBK，打印 ✅/❌ 会抛 UnicodeEncodeError 打断登录流程
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from app import oauth
from app.config import PROJECT_ROOT, ConfigError, load_config
from app.store import Credential, MODE_OAUTH, load_auth_dir


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CodeArts OAuth2 (PKCE + DPoP) 登录")
    parser.add_argument("--print-only", action="store_true", help="只打印链接，走 ticket 轮询（服务器模式）")
    parser.add_argument("--auth-dir", default="", help="凭证输出目录（默认取配置的 auth_dir）")
    parser.add_argument("--client-id", default="", help="OAuth client_id（默认 codearts-agent 或配置值）")
    parser.add_argument("--timeout", type=float, default=oauth.LOGIN_TIMEOUT_SECONDS, help="登录超时秒数")
    args = parser.parse_args(argv)

    try:
        # 登录阶段还没有凭证，require_credentials=False 才不会被「没有凭证」挡住；
        # require_api_key=False 是因为「先登录拿凭证、再配 API Key」是正常顺序。
        cfg = load_config(require_credentials=False, require_api_key=False)
    except ConfigError as error:
        print("配置错误：", error, file=sys.stderr)
        return 2

    auth_dir = args.auth_dir or cfg.auth_dir
    if not os.path.isabs(auth_dir):
        auth_dir = os.path.join(PROJECT_ROOT, auth_dir)

    # client_id 优先取已有账号记录：refresh_token 与 client_id 绑定，
    # 混用会让老账号刷新失败（invalid client id）。
    client_id = args.client_id or cfg.login_client_id
    if not client_id:
        for cred in load_auth_dir(auth_dir):
            if cred.client_id:
                client_id = cred.client_id
                break
    login_cfg = oauth.default_login_config(client_id)

    try:
        result, dpop_jwk, verifier = oauth.login_interactive(
            login_cfg, print_only=args.print_only, timeout=args.timeout
        )
    except oauth.OAuthError as error:
        print(f"\n登录失败：{error}", file=sys.stderr)
        return 1

    cred = Credential(
        uid=result.user_id,
        user_name=result.user_name,
        domain_id=result.domain_id,
        mode=MODE_OAUTH,
        access_key_id=result.access_key_id,
        secret_access_key=result.secret_access_key,
        security_token=result.security_token,
        expiration=result.expiration,
        refresh_token=result.refresh_token,
        code_verifier=verifier,
        client_id=login_cfg.client_id,
        dpop_private_key=dpop_jwk,
    )
    if not cred.uid:
        # portal 回调可能不带身份：用 STS 凭证查一次，再退化到 refresh_token 的 sub
        uid, name, domain = oauth.lookup_identity(cred.sign_credential())
        cred.uid = uid or oauth.identity_from_refresh_token(result.refresh_token)
        cred.user_name = cred.user_name or name
        cred.domain_id = cred.domain_id or domain
    if not cred.uid:
        cred.uid = "unknown-" + oauth.random_hex(4)

    os.makedirs(auth_dir, exist_ok=True)
    cred.path = os.path.join(auth_dir, cred.file_name())
    cred.save()

    print(f"\n✅ 登录成功：user_id={cred.uid} name={cred.user_name or '未命名'}")
    print(f"凭证已保存：{cred.path}")
    if cred.expiration:
        print(f"STS 有效期至：{cred.expiration}（到期前服务会自动 refresh 续期）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
