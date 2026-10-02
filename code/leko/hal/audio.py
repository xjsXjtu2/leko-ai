"""HAL：音频（绿联一体机 USB 声卡）。原子能力：采集/播放设备、音量、麦克风流、段电平。"""
from __future__ import annotations

import math
import re
import subprocess
import sys

from leko.core.util import run

_UGREEN = r"card (\d+)[^\n]*(?:ugreen|绿联|camera)"
_PLAY_DEV: str | None = None


def alsa_card(subcmd=("aplay", "-l")) -> int | None:
    _, out, _ = run(list(subcmd))
    m = re.search(_UGREEN, out, re.I)
    return int(m.group(1)) if m else None


def ensure_volume():
    """绿联声卡 PCM 默认 0%（04_volume_ctl 的发现），best-effort 设 100%。"""
    if sys.platform == "darwin":
        return
    card = alsa_card()
    if card is not None:
        rc, _, _ = run(["amixer", "-c", str(card), "set", "PCM", "100%", "unmute"])
        if rc == 0:
            print(f"   🔊 声卡 {card} PCM → 100%（还嫌小是 1W 喇叭的物理极限，户外等 ReSpeaker+3W）")


def capture_dev() -> str:
    _, out, _ = run(["arecord", "-l"])
    m = re.search(_UGREEN, out, re.I)
    return f"plughw:{m.group(1)},0" if m else "default"


def playback_dev() -> str:
    """播放设备：直连绿联声卡 plughw。默认路由下 aplay 会 audio open error 524
    （PipeWire 占着默认设备），ffplay 走 PipeWire 反而正常——所以绕开默认路由。"""
    global _PLAY_DEV
    if _PLAY_DEV is None:
        _, out, _ = run(["aplay", "-l"])
        m = re.search(_UGREEN, out, re.I)
        _PLAY_DEV = f"plughw:{m.group(1)},0" if m else "default"
    return _PLAY_DEV


def aplay(wav) -> bool:
    """aplay 直连绿联卡播放 wav；失败把报错打出来（别再静默吞掉）。"""
    rc, _, err = run(["aplay", "-q", "-D", playback_dev(), str(wav)])
    if rc != 0:
        print(f"   ⚠ 播放失败（{playback_dev()}）：{err.strip()[:100]}")
    return rc == 0


def start_mic():
    """打开 16k 单声道原始流麦克风（arecord）。"""
    dev = capture_dev()
    print(f"   🎤 采集设备：{dev}")
    return subprocess.Popen(
        ["arecord", "-D", dev, "-q", "-t", "raw", "-f", "S16_LE",
         "-r", "16000", "-c", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=-1)


def seg_dbfs(samples) -> float:
    """语音段 RMS 电平（dBFS）。samples 可能是 int16 刻度或归一化刻度，按峰值自适应。"""
    import numpy as np
    a = np.abs(np.asarray(samples, dtype=np.float32))
    if a.size == 0:
        return -99.0
    scale = 32768.0 if float(a.max()) > 8.0 else 1.0
    rms = float(np.sqrt(np.mean(np.square(a / scale))))
    return 20 * math.log10(rms) if rms > 0 else -99.0
