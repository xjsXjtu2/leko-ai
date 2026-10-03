"""把送去识别的那一句存成 MP3，并按体积、时间删最旧的。

VAD 段原本只活在内存里。这里在后台交给 ffmpeg（libmp3lame，64kb/s），
不挡识别。目录见 paths.vad_clips。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from leko.core.config import CONFIG, path
from leko.voice.asr import _pcm16

_lock = threading.Lock()
_warned = False
_seq = 0
_MP3 = (".mp3", ["-c:a", "libmp3lame", "-b:a", "64k", "-ar", "44100", "-f", "mp3"])


def keep_vad_clip(samples, text: str = "") -> Path | None:
    """拷一份 PCM 丢到后台编码。立刻返回目标路径；没有编码器时返回 None。"""
    if _codec_args() is None:
        return None
    pcm = _pcm16(samples)
    if not pcm:
        return None
    dest = _dest(text)
    threading.Thread(target=_write, args=(pcm, dest), daemon=True).start()
    return dest


def _dest(text: str) -> Path:
    global _seq
    tag = re.sub(r"[^\w]+", "", text, flags=re.UNICODE)[:20]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    with _lock:
        _seq += 1
        n = _seq
    name = f"{stamp}-{n:02d}-{tag}.mp3" if tag else f"{stamp}-{n:02d}.mp3"
    return path("vad_clips") / name


def _codec_args() -> tuple[str, list[str]] | None:
    global _warned
    if shutil.which("ffmpeg") is None:
        if not _warned:
            print("   ⚠ 没有 ffmpeg，识别片段不落盘")
            _warned = True
        return None
    return _MP3


def _write(pcm: bytes, dest: Path) -> None:
    global _warned
    spec = _codec_args()
    if spec is None:
        return
    suffix, args = spec
    dest = dest.with_suffix(suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(suffix + ".part")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0", *args, str(part)]
    try:
        subprocess.run(cmd, input=pcm, check=True, capture_output=True)
        part.replace(dest)
    except subprocess.CalledProcessError as e:
        part.unlink(missing_ok=True)
        err = (e.stderr or b"").decode("utf-8", "replace")
        if not _warned:
            print(f"   ⚠ 识别片段编码失败，这一句不落盘（{err.strip()[:120]}）")
            _warned = True
        return
    _prune(dest.parent)


def _prune(directory: Path) -> None:
    keep = CONFIG.get("vad_clips") or {}
    max_bytes = int(float(keep.get("max_mb", 20)) * 1024 * 1024)
    max_age = float(keep.get("max_hours", 48)) * 3600
    now = time.time()
    with _lock:
        files = [p for p in directory.iterdir()
                 if p.is_file() and p.suffix in {".mp3", ".ogg", ".m4a"}]
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files:
            if now - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
        files = [p for p in files if p.exists()]
        total = sum(p.stat().st_size for p in files)
        while files and total > max_bytes:
            old = files.pop(0)
            total -= old.stat().st_size
            old.unlink(missing_ok=True)
        for p in directory.glob("*.part"):
            if now - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
