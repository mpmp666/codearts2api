# -*- coding: utf-8 -*-
"""原子写文件：唯一临时名 + 瞬时失败重试。

**为什么不能直接用 `path + ".tmp"`：**

1. **固定临时名在并发保存时会互相踩。** 进程内可能有多个实例指向同一个路径
   （`ChatStore`/`StateStore`/`AuthStore` 的锁是**实例级**的，实例之间不互斥），
   A 刚写完正在 `os.replace`，B 又用同一个临时名打开写 —— Windows 上
   `os.replace` 直接抛 `PermissionError [WinError 32]`（POSIX 上则是静默互相覆盖内容，
   更隐蔽）。实测：测试套件全量跑时 `data/chats.json` 就撞在这个点上，
   单独跑该用例却必过 —— 典型的并发竞态特征。

2. **Windows 上 `os.replace` 对「目标文件被短暂打开」很敏感**（索引器、杀软、
   同进程的读者都算）。这是瞬时错误，重试几次即可；POSIX 语义下原子替换不受影响，
   所以这类 bug 只在 Windows 暴露，很容易漏到线上。

凭证类文件额外需要 `0600`（临时文件阶段就收紧，避免 rename 前后有可读窗口）。
"""
from __future__ import annotations

import json
import os
import threading
import time

# 临时名里的序号：同一线程连续保存也不会重名
_SEQ = 0
_SEQ_LOCK = threading.Lock()


def _temp_path(path: str) -> str:
    """生成唯一的临时文件名（pid + 线程 id + 自增序号）。

    刻意**不用** `tempfile.mkstemp`：它返回已打开的 fd，我们要自己控制
    「写完→chmod→rename」的顺序，且在 rename 前不能有其它句柄开着。
    """
    global _SEQ
    with _SEQ_LOCK:
        _SEQ += 1
        seq = _SEQ
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{seq}.tmp"


def write_bytes_atomic(path: str, data: bytes, mode: int | None = None,
                       retries: int = 6) -> None:
    """把 data 原子写入 path。

    mode 非空时在 rename **之前**给临时文件设权限（凭证文件用 0o600）。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp = _temp_path(path)
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
        if mode is not None:
            try:
                os.chmod(temp, mode)
            except OSError:
                # 权限设置失败不该阻断落盘（某些文件系统不支持 chmod）
                pass
        last: OSError | None = None
        for attempt in range(retries):
            try:
                os.replace(temp, path)
                return
            except PermissionError as error:
                # Windows：目标被短暂占用。退避重试；最后一次仍失败才抛出。
                last = error
                time.sleep(0.02 * (attempt + 1))
        if last is not None:
            raise last
    finally:
        # rename 成功后 temp 已不存在；失败路径要清掉，避免留下垃圾
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def write_json_atomic(path: str, payload, mode: int | None = None,
                      indent: int | None = 2) -> None:
    """JSON 版本的原子写（统一 ensure_ascii=False，中文可读）。"""
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    write_bytes_atomic(path, text.encode("utf-8"), mode=mode)
