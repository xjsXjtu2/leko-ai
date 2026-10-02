"""通用小工具：子进程、交互提问。"""
from __future__ import annotations

import subprocess
import sys


def run(cmd, timeout=30):
    """跑命令 → (rc, stdout, stderr)，永不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:                                  # noqa: BLE001
        return 1, "", str(e)


def ask(prompt: str, hint: str = ""):
    """交互提问；非终端直接退出（input() 在 ssh/管道下会卡死）。"""
    if not sys.stdin.isatty():
        sys.exit("✗ 需要交互终端" + (f"；{hint}" if hint else ""))
    return input(prompt)
