"""云端语音识别：百炼 qwen3-asr-flash（同步，短指令）。

API key 只在 ECS .env。音频用 data URL 直传，不落盘。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from vision import UpstreamError

_ENDPOINT = ("https://dashscope.aliyuncs.com/api/v1/services/"
             "aigc/multimodal-generation/generation")
# 官方的上下文提示，不是本地谐音替换。帮助短指令里的「米 / 后」稳定下来。
_CONTEXT = "这是给机器人的短指令。常见说法：前进一米，后退一米，左转，右转，掉头，停，听写。"


def transcribe(wav_b64: str) -> str:
    """16k 单声道 wav 的 base64 → 识别文本。失败抛 UpstreamError。"""
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise UpstreamError("DASHSCOPE_API_KEY 未配置")
    data_uri = wav_b64 if wav_b64.startswith("data:") else f"data:audio/wav;base64,{wav_b64}"
    body = {
        "model": os.environ.get("ASR_MODEL", "qwen3-asr-flash"),
        "input": {"messages": [
            {"role": "system", "content": [{"text": _CONTEXT}]},
            {"role": "user", "content": [{"audio": data_uri}]},
        ]},
        "parameters": {"asr_options": {"language": "zh", "enable_itn": False}},
    }
    req = urllib.request.Request(
        _ENDPOINT, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise UpstreamError(f"百炼 {e.code}：{detail}") from e
    except Exception as e:                                  # noqa: BLE001
        raise UpstreamError(f"百炼调用失败：{e}") from e
    return _text_of(data)


def _text_of(data: dict) -> str:
    choices = (data.get("output") or {}).get("choices") or data.get("choices") or []
    if not choices:
        raise UpstreamError(f"百炼返回没有 choices：{str(data)[:200]}")
    content = ((choices[0].get("message") or {}).get("content"))
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(p.get("text", "")).strip() for p in content if isinstance(p, dict)]
        return "".join(parts).strip()
    raise UpstreamError(f"认不出的识别结果：{str(content)[:200]}")
