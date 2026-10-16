# -*- coding: utf-8 -*-
"""本地联调脚本：通过运行中的代理调用 CodeArts 模型。

用法（先启动 `python server.py`）：

    python test_openai.py                    # 列表 + 非流式 + 流式
    python test_openai.py --model GLM-5.2    # 指定模型
    python test_openai.py --base http://127.0.0.1:8787/v1

API Key 从环境变量 `CA2A_API_KEY` 或 `.env` 读取（增强版起 `/v1/*` 必须带 Key，
基线那会儿是任意字符串，现在必须与配置一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

API_KEY = os.getenv("CA2A_API_KEY", "").strip()
DEFAULT_BASE = "http://127.0.0.1:8787/v1"


def headers(extra=None):
    head = {"Content-Type": "application/json"}
    if API_KEY:
        head["Authorization"] = "Bearer " + API_KEY
    head.update(extra or {})
    return head


def call(url, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers=headers(), method="POST" if data else "GET"
    )
    return urllib.request.urlopen(request, timeout=timeout)


def test_models(base):
    print("== GET /models ==")
    with call(base + "/models") as response:
        payload = json.loads(response.read())
    for model in payload.get("data", []):
        flag = "（限时福利）" if model.get("benefit") else ""
        print(f"  {model['id']}{flag}")
    return [model["id"] for model in payload.get("data", [])]


def test_chat(base, model):
    print(f"\n== POST /chat/completions（非流式, model={model}）==")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复：成功"}],
        "stream": False,
        "max_tokens": 64,
    }
    try:
        with call(base + "/chat/completions", payload) as response:
            data = json.loads(response.read())
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}")


def test_stream(base, model):
    print(f"\n== POST /chat/completions（流式, model={model}）==")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "数到三"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 64,
    }
    try:
        with call(base + "/chat/completions", payload) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    print("\n[DONE]")
                    break
                try:
                    chunk = json.loads(body)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    if chunk.get("usage"):
                        print(f"\n[usage] {chunk['usage']}")
                    continue
                delta = choices[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    sys.stdout.write(delta["reasoning_content"])
                if delta.get("content"):
                    sys.stdout.write(delta["content"])
                sys.stdout.flush()
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="通过代理联调 CodeArts 模型")
    parser.add_argument("--base", default=DEFAULT_BASE, help="代理的 OpenAI Base URL")
    parser.add_argument("--model", default="", help="指定模型（默认取列表第一个）")
    args = parser.parse_args()

    if not API_KEY:
        print("提示: 未设置 CA2A_API_KEY，请求会返回 401。", file=sys.stderr)

    models = test_models(args.base)
    if not models:
        print("模型列表为空：请检查账号是否可用（python login.py 或 .env 的 AK/SK）。")
        return 1
    model = args.model or models[0]
    test_chat(args.base, model)
    test_stream(args.base, model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
