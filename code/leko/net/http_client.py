"""net：对 ECS 视觉服务的最小 HTTP 客户端（urllib，零依赖）。

Bearer token：config.yaml dictation.device_token，或仓库根 device_token 文件
（gitignore，不进 git）。失败抛 RuntimeError，上层语音降级。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from leko.core.config import CONFIG, REPO_ROOT


def _token() -> str:
    tok = CONFIG["dictation"].get("device_token") or ""
    if not tok:
        f = REPO_ROOT / "device_token"
        if f.exists():
            tok = f.read_text().strip()
    return tok


def post_json(path: str, payload: dict, timeout: int = 30) -> dict:
    """POST JSON → dict；401/503/网络失败 抛 RuntimeError（中文可播报）。"""
    base = str(CONFIG["dictation"]["ecs_base"]).rstrip("/")
    url = base + path
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:                                   # noqa: BLE001
            pass
        if e.code == 503:
            raise RuntimeError("云端还没配好，等妈妈配完再试") from e
        if e.code == 401:
            raise RuntimeError("设备令牌不对，检查 device_token") from e
        raise RuntimeError(f"云端返回 {e.code}：{detail}") from e
    except Exception as e:                                   # noqa: BLE001
        raise RuntimeError(f"连不上云端（网络不好？）") from e


def get_health(timeout: int = 5) -> dict:
    base = str(CONFIG["dictation"]["ecs_base"]).rstrip("/")
    req = urllib.request.Request(base + "/health")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
