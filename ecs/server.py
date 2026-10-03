"""ECS 视觉服务：英语听写 F2.0（08-英语听写技术方案 §五）。

纯函数 HTTP 服务：词表提取 / 手写判分。API key 只在本机 .env，Pi 零密钥。
以后上 dsh，vision.py 两个函数原样搬进 robot-gateway 插件（07 退路设计）。

部署（root@8.161.228.205）：
  cd ~/leko-ecs && python3 -m venv venv && venv/bin/pip install -r requirements.txt
  cp .env.example .env && vim .env        # 填 DASHSCOPE_API_KEY
  cp leko-vision.service /etc/systemd/system/ && systemctl daemon-reload
  systemctl enable --now leko-vision
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

from typing import List

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

import asr
import prompts
import vision

app = FastAPI(title="leko-vision", docs_url=None, redoc_url=None)
TOKEN = os.environ.get("DEVICE_TOKEN", "")
DB_PATH = Path(__file__).parent / "leko.db"


def _auth(request: Request):
    got = request.headers.get("Authorization", "")
    import hmac
    if not TOKEN or not hmac.compare_digest(got, f"Bearer {TOKEN}"):
        raise HTTPException(401, "bad token")


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS dictation_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,
        words TEXT, results TEXT)""")
    return con


class WordlistReq(BaseModel):
    image_b64: str


class AsrReq(BaseModel):
    wav_b64: str


class HandwritingReq(BaseModel):
    image_b64: str
    words: List[str]


@app.get("/health")
def health():
    return {"ok": True, "api_key": bool(os.environ.get("DASHSCOPE_API_KEY")),
            "model": os.environ.get("VISION_MODEL", "qwen-vl-plus"),
            "asr_model": os.environ.get("ASR_MODEL", "qwen3-asr-flash")}


@app.post("/asr")
def transcribe(req: AsrReq, request: Request):
    """唤醒之后的一句指令。key 未配返回 503，Pi 退回本地 SenseVoice。"""
    _auth(request)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise HTTPException(503, "DASHSCOPE_API_KEY 未配置（ECS ~/leko-ecs/.env）")
    try:
        text = asr.transcribe(req.wav_b64)
    except asr.UpstreamError as e:
        raise HTTPException(502, str(e))
    return {"text": text}


@app.post("/vision/wordlist")
def wordlist(req: WordlistReq, request: Request):
    _auth(request)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise HTTPException(503, "DASHSCOPE_API_KEY 未配置（ECS ~/leko-ecs/.env）")
    try:
        words = vision.extract_wordlist(req.image_b64)
    except vision.UpstreamError as e:
        raise HTTPException(502, str(e))
    except vision.BadOutput as e:
        raise HTTPException(422, f"模型输出不合规：{e}")
    return {"words": words}


@app.post("/vision/handwriting")
def handwriting(req: HandwritingReq, request: Request):
    _auth(request)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise HTTPException(503, "DASHSCOPE_API_KEY 未配置（ECS ~/leko-ecs/.env）")
    try:
        words = vision.judge_handwriting(req.image_b64, req.words)
    except vision.UpstreamError as e:
        raise HTTPException(502, str(e))
    except vision.BadOutput as e:
        raise HTTPException(422, f"模型输出不合规：{e}")
    # 服务端兜底：expect 必须与请求词表一致，对不上的一律 unsure（不冤枉孩子）
    fixed, valid = [], {w.lower(): w for w in req.words}
    for item in words:
        expect = valid.get(str(item.get("expect", "")).lower())
        if expect is None:
            fixed.append({"expect": str(item.get("expect", "?")),
                          "verdict": "unsure", "wrote": item.get("wrote", "")})
        else:
            item["expect"] = expect
            if item.get("verdict") not in ("correct", "wrong", "unsure"):
                item["verdict"] = "unsure"
            fixed.append(item)
    try:
        con = _db()
        con.execute("INSERT INTO dictation_sessions(ts, words, results) VALUES (?,?,?)",
                    (time.time(), ",".join(req.words), str(fixed)))
        con.commit()
        con.close()
    except Exception:                                     # noqa: BLE001  存档失败不影响判分
        pass
    return {"words": fixed, "unsure": sum(1 for w in fixed if w["verdict"] == "unsure")}
