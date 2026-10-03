#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_av_test.py —— 绿联一体机（摄像头 + 双麦 + 扬声器）验收测试

对应文档：
  06-第二批采购清单-音视频-v1.md  →「到货验收」三条目
  07-软件架构设计-v1.0-cursor-ac.md §3.4「一体机的现实约束」

设计原则：
  1. 只用 Python 标准库（无需 pip 安装任何包）；采集/播放借外部命令。
  2. 跨平台：macOS（avfoundation）+ 树莓派 Linux（v4l2 / ALSA 或 PulseAudio）。
  3. 每个结论都由可复算的数字支撑（RMS dBFS、拉普拉斯方差、帧计数），
     不是"看起来正常"。所有产物落在 code/av_out/ 便于复查。

用法：
  python3 code/04_av_test.py deps          # 依赖与设备自检（先跑这个）
  python3 code/04_av_test.py list          # 列出采集/播放设备
  python3 code/04_av_test.py camera        # 摄像头：分辨率/型号判定/自动对焦/帧率
  python3 code/04_av_test.py mic           # 麦克风：录音/电平/双通道/静音比
  python3 code/04_av_test.py mic --distance # 拾音距离衰减（0.5/1/2/3m，交互）
  python3 code/04_av_test.py speaker       # 扬声器：1kHz/扫频/粉噪/左右声道/中文播报
  python3 code/04_av_test.py speaker --spl # 声压测试记录（对照 06 文档实验室数据）
  python3 code/04_av_test.py selftest      # 无硬件自检（验证波形生成与算法）
  python3 code/04_av_test.py all           # 全套 + 生成报告 code/av_out/report.md

常用参数：
  --device NAME     指定设备（默认按关键词 UGREEN / 绿联 自动匹配）
  --seconds N       录音/录像时长（默认 5）
  --out DIR         产物目录（默认 code/av_out）
  --no-play         只生成音频不播放
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import platform
import random
import re
import shutil
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 常量与配置
# --------------------------------------------------------------------------

KEYWORDS = ("ugreen", "绿联", "camera audio", "camera 2k", "65545", "camera")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 分辨率探测顺序：由高到低；用于判定 2K 款（65545）还是 1080p 款（45644）
RESOLUTIONS = ["2560x1440", "2592x1520", "1920x1080", "1280x720"]
RES_2K = {"2560x1440", "2592x1520"}


def px_of(size: str) -> int:
    """'2560x1440' → 像素总数；用于与探测顺序无关地取「最高」分辨率。"""
    try:
        w, h = (int(x) for x in size.lower().split("x"))
        return w * h
    except Exception:                                          # noqa: BLE001
        return 0

# 06 文档记录的实验室数据（绿联官方，1kHz 测试音）
SPL_REFERENCE = [("0 m", 118), ("0.5 m", 86), ("1 m", 76)]

SILENCE_DBFS = -50.0     # 低于此视为静音
CLIP_LEVEL = 32760       # 削波判定
FRAME_MS = 20            # 静音统计帧长


def C(s: str, code: str) -> str:
    """极简着色（不依赖第三方库）。NO_COLOR 或非 TTY 时原样返回。"""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    codes = {"g": "32", "y": "33", "r": "31", "b": "36", "d": "90", "bold": "1"}
    return f"\033[{codes.get(code, '0')}m{s}\033[0m"


def ok(s):    return C("  ✓ ", "g") + s
def bad(s):   return C("  ✗ ", "r") + s
def warn(s):  return C("  ! ", "y") + s
def info(s):  return C("  · ", "d") + s
def head(s):  print("\n" + C(s, "bold"))


def run(cmd, timeout=60, check=False):
    """执行外部命令，返回 (returncode, stdout, stderr)。永不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except Exception as e:                                    # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"


def have(exe: str) -> bool:
    return shutil.which(exe) is not None


# --------------------------------------------------------------------------
# 信号分析（纯标准库）
# --------------------------------------------------------------------------

def samples_stats(samples: array.array) -> dict:
    """对 int16 采样序列算电平、静音比、削波比。"""
    n = len(samples)
    if n == 0:
        return {"n": 0, "rms_dbfs": -999.0, "peak_dbfs": -999.0,
                "silence_ratio": 1.0, "clip_ratio": 0.0}
    peak = 0
    acc = 0.0
    clipped = 0
    for s in samples:
        a = -s if s < 0 else s
        if a > peak:
            peak = a
        if a >= CLIP_LEVEL:
            clipped += 1
        acc += float(s) * float(s)
    rms = math.sqrt(acc / n)

    def to_dbfs(v):
        return 20 * math.log10(v / 32768.0) if v > 0 else -999.0

    # 分帧静音统计
    fl = max(1, int(16000 * FRAME_MS / 1000))
    silent = total = 0
    for i in range(0, n - fl + 1, fl):
        seg = samples[i:i + fl]
        e = math.sqrt(sum(float(x) * x for x in seg) / len(seg))
        total += 1
        if e <= 0 or 20 * math.log10(e / 32768.0) < SILENCE_DBFS:
            silent += 1
    return {
        "n": n,
        "rms_dbfs": round(to_dbfs(rms), 1),
        "peak_dbfs": round(to_dbfs(peak), 1),
        "silence_ratio": round(silent / total, 3) if total else 1.0,
        "clip_ratio": round(clipped / n, 5),
    }


def read_wav_mono(path: Path):
    """读 WAV，返回 (rate, channels, 每通道 int16 array 列表)。"""
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f"只支持 16-bit WAV，实际 {width * 8}-bit")
    all_s = array.array("h")
    all_s.frombytes(raw)
    if sys.byteorder == "big":
        all_s.byteswap()
    chans = [all_s[c::ch] for c in range(ch)]
    return rate, ch, chans


def laplacian_var(gray: bytes, w: int, h: int) -> float:
    """灰度原始帧的拉普拉斯方差 —— 清晰度指标，越大越清晰（对焦准）。"""
    if w < 3 or h < 3 or len(gray) < w * h:
        return 0.0
    vals = []
    stride = w
    for y in range(1, h - 1):
        row = y * stride
        for x in range(1, w - 1):
            i = row + x
            v = (gray[i - 1] + gray[i + 1] + gray[i - stride] + gray[i + stride]
                 - 4 * gray[i])
            vals.append(v)
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / len(vals)
    return var


# --------------------------------------------------------------------------
# 设备发现
# --------------------------------------------------------------------------

def parse_mac_devices():
    """解析 ffmpeg avfoundation 设备表；ffmpeg 缺失时返回 None。"""
    if not have("ffmpeg"):
        return None
    rc, _, err = run(["ffmpeg", "-hide_banner", "-f", "avfoundation",
                      "-list_devices", "true", "-i", ""], timeout=20)
    text = err
    video, audio, section = [], [], None
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            section = "v"
            continue
        if "AVFoundation audio devices" in line:
            section = "a"
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m and section:
            (video if section == "v" else audio).append(
                {"index": int(m.group(1)), "name": m.group(2).strip()})
    return {"video": video, "audio": audio}


def mac_system_profiler():
    """不依赖 ffmpeg 的设备信息（用于确认绿联设备是否插上）。"""
    out = {"audio": [], "camera": []}
    if not IS_MAC:
        return out
    rc, o, _ = run(["system_profiler", "SPAudioDataType"], timeout=60)
    cur = None
    for line in o.splitlines():
        if re.match(r"^\s{8}\S.*:\s*$", line):
            cur = {"name": line.strip().rstrip(":"), "default_out": False,
                   "default_in": False, "rate": None, "channels": None}
            out["audio"].append(cur)
        elif cur is not None:
            if "Default Output Device: Yes" in line:
                cur["default_out"] = True
            if "Default Input Device: Yes" in line:
                cur["default_in"] = True
            m = re.search(r"Current SampleRate:\s*(\d+)", line)
            if m:
                cur["rate"] = int(m.group(1))
            m = re.search(r"(Output|Input) Channels:\s*(\d+)", line)
            if m:
                cur["channels"] = int(m.group(2))
            if cur.get("default_out") or cur.get("default_in"):
                cur["channels"] = cur["channels"] or 2
    rc, o, _ = run(["system_profiler", "SPCameraDataType"], timeout=60)
    for line in o.splitlines():
        m = re.match(r"^\s{4}(\S.*?):\s*$", line)
        if m:
            out["camera"].append({"name": m.group(1).strip()})
        m = re.search(r"Model ID:\s*(.+)$", line)
        if m and out["camera"]:
            out["camera"][-1]["model"] = m.group(1).strip()
    return out


# ALSA 设备行样例：
#   card 1: Camera [UGREEN Camera], device 0: USB Audio [USB Audio]
#   card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
# card 名和 device 名都可能是多词，所以 device 名不能用 \S+ 去卡（会因空格而整行匹配失败）。
_ALSA_LINE_RX = re.compile(
    r"card\s+(\d+):\s+(\S+)\s+\[(.*?)\]\s*,\s*"
    r"device\s+(\d+):\s*(.*?)\s*\[(.*?)\]\s*$")


def _parse_alsa_text(text: str) -> list[dict]:
    """把 arecord -l / aplay -l 的输出解析成设备表（容错）。"""
    out, seen = [], set()
    for line in text.splitlines():
        m = _ALSA_LINE_RX.search(line)
        if not m:
            continue
        card, card_id, card_name, dev, dev_id, dev_name = (
            g.strip() for g in m.groups())
        if (card, dev) in seen:                 # 同一 card/dev 只留一条
            continue
        seen.add((card, dev))
        out.append({
            "card": int(card), "card_id": card_id, "card_name": card_name,
            "dev": int(dev), "dev_id": dev_id, "dev_name": dev_name,
            "name": f"{card_id} [{card_name}] / {dev_name}",
            "plughw": f"plughw:{card},{dev}",
        })
    return out


def parse_linux_audio():
    """arecord -l / aplay -l → ALSA 设备列表；同时保留原始输出用于排障。"""
    devs = {"capture": [], "playback": [], "raw": {}}
    for cmd, key in ((["arecord", "-l"], "capture"), (["aplay", "-l"], "playback")):
        if not have(cmd[0]):
            devs["raw"][key] = f"{cmd[0]} 不存在"
            continue
        rc, o, e = run(cmd, timeout=20)
        devs["raw"][key] = (o + e).strip()
        devs[key] = _parse_alsa_text(o)
    return devs


def alsa_stream_info(card) -> dict:
    """读 /proc/asound/cardN/stream0 → USB 音频的原生能力（采集/播放各自的速率与声道）。

    **只读文件，不打开设备**。这一点很重要：用 arecord/--dump-hw-params 去探能力会真的
    打开 hw 设备，某些 UAC 摄像头（本项目实测）被非法参数打开后采集流会卡死、此后一直
    输出全零，必须复位 USB 才能恢复。所以这里坚持只读。
    """
    if not (IS_LINUX and card is not None):
        return {}
    try:
        txt = Path(f"/proc/asound/card{card}/stream0").read_text(
            encoding="utf-8", errors="replace")
    except Exception:                                          # noqa: BLE001
        return {}
    out, cur = {}, None
    for line in txt.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("playback:") or low.startswith("capture:"):
            cur = low[:-1]
            out[cur] = {}
            continue
        if cur is None:
            continue
        m = re.match(r"(Channels|Rates|Format|Bits):\s*(.+)$", s)
        if m:
            out[cur].setdefault(m.group(1).lower(), m.group(2).strip())
    return out


def alsa_playback_volume(card):
    """ALSA 播放音量 → (百分比, 是否未静音)；拿不到返回 (None, None)。

    很多 USB 音频的播放音量默认是 0%：aplay 会返回成功，但一点声音都没有。
    """
    if not (IS_LINUX and have("amixer") and card is not None):
        return None, None
    rc, o, e = run(["amixer", "-c", str(card), "sget", "PCM"], timeout=10)
    txt = o + e
    m = re.search(r"\[(\d+)%\]", txt)
    if not m:
        return None, None
    return int(m.group(1)), ("[off]" not in txt.lower())


def fix_playback_volume(card) -> tuple:
    """把 ALSA 播放音量顶到 100% 并取消静音。

    设混音器不需要 root（`alsactl store` 才需要），所以能自动修就自动修，
    别让用户对着一段「播放成功但没声」的日志猜。
    """
    if not (IS_LINUX and have("amixer") and card is not None):
        return False, "无 amixer 或未定位到声卡"
    rc, _, e = run(["amixer", "-c", str(card), "sset", "PCM", "100%", "unmute"],
                   timeout=10)
    if rc != 0:
        return False, (e.strip().splitlines() or ["amixer 执行失败"])[-1][:60]
    vol, unmuted = alsa_playback_volume(card)
    if vol == 100 and unmuted:
        return True, ""
    return False, f"设置后仍为 {vol}%"


def read_alsa_cards() -> str:
    """/proc/asound/cards 是最权威的 ALSA 卡清单，排障用。"""
    try:
        return Path("/proc/asound/cards").read_text(
            encoding="utf-8", errors="replace").strip()
    except Exception:                                          # noqa: BLE001
        return ""


def alsa_hit(d) -> bool:
    """该 ALSA 设备是否像绿联一体机的音频口。"""
    blob = " ".join(str(d.get(k, "")) for k in
                    ("card_id", "card_name", "dev_name")).lower()
    return any(k in blob for k in ("ugreen", "绿联", "camera", "usb audio"))


def parse_linux_video():
    """v4l2-ctl --list-devices，退回 /dev/video* 列表。"""
    out = []
    if have("v4l2-ctl"):
        rc, o, _ = run(["v4l2-ctl", "--list-devices"], timeout=20)
        name = None
        for line in o.splitlines():
            if line and not line.startswith(("\t", " ")):
                name = line.rstrip(":").strip()
            elif "/dev/video" in line and name:
                out.append({"name": name, "dev": line.strip()})
    if not out:
        for p in sorted(Path("/dev").glob("video*")):
            out.append({"name": "unknown", "dev": str(p)})
    return out


def parse_pulse(kind: str):
    """pactl list short <kind> → PulseAudio/PipeWire 设备。"""
    if not have("pactl"):
        return []
    rc, o, _ = run(["pactl", "list", "short", kind], timeout=20)
    res = []
    for line in o.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            res.append({"idx": parts[0], "name": parts[1],
                        "desc": parts[2] if len(parts) > 2 else parts[1]})
    return res


def match_device(items, keyword, name_key="name"):
    """按关键词匹配设备；返回 (选中项, 全部候选)。"""
    kw = (keyword or "").lower()
    if kw:
        for it in items:
            if kw in str(it.get(name_key, "")).lower():
                return it, items
        return None, items
    for k in KEYWORDS:
        for it in items:
            if k in str(it.get(name_key, "")).lower():
                return it, items
    return None, items


def survey():
    """汇总当前平台的设备与后端能力。"""
    s = {"platform": "macOS" if IS_MAC else ("Linux" if IS_LINUX else sys.platform),
         "tools": {}, "video": [], "capture": [], "playback": [], "notes": []}
    for t in ("ffmpeg", "ffprobe", "afplay", "say", "arecord", "aplay",
              "v4l2-ctl", "pactl", "paplay", "parecord", "switchaudio"):
        s["tools"][t] = have(t)
    if IS_MAC:
        sp = mac_system_profiler()
        s["sp_audio"] = sp["audio"]
        s["sp_camera"] = sp["camera"]
        av = parse_mac_devices()
        if av is None:
            s["notes"].append("ffmpeg 未安装 → 无法列出 avfoundation 索引，也无法采集")
        else:
            s["video"] = av["video"]
            s["capture"] = av["audio"]
            s["playback"] = av["audio"]
    elif IS_LINUX:
        s["video"] = parse_linux_video()
        alsa = parse_linux_audio()
        s["capture"] = alsa["capture"]
        s["playback"] = alsa["playback"]
        s["alsa_raw"] = alsa["raw"]
        s["alsa_cards"] = read_alsa_cards()
        s["pulse_sources"] = parse_pulse("sources")
        s["pulse_sinks"] = parse_pulse("sinks")
    return s


def backend_ready(kind: str):
    """该平台上采集/播放所需的外部命令是否齐备，返回 (bool, 缺失说明)。"""
    missing = []
    if kind in ("camera", "mic"):
        if IS_MAC and not have("ffmpeg"):
            missing.append("ffmpeg")
        if IS_LINUX and kind == "camera" and not (have("ffmpeg") or have("v4l2-ctl")):
            missing.append("ffmpeg（视频采集）")
        if IS_LINUX and kind == "mic" and not (have("ffmpeg") or have("arecord") or have("parecord")):
            missing.append("ffmpeg 或 alsa-utils(arecord) 或 pulseaudio-utils(parecord)")
    if kind == "speaker":
        if IS_MAC and not (have("afplay") or have("ffmpeg")):
            missing.append("afplay（macOS 自带）或 ffmpeg")
        if IS_LINUX and not (have("aplay") or have("paplay") or have("ffmpeg")):
            missing.append("alsa-utils(aplay) 或 pulseaudio-utils(paplay) 或 ffmpeg")
    return (not missing), ", ".join(missing)


INSTALL_HINT = {
    "macOS": [
        "brew install ffmpeg            # 视频+音频采集/生成（一次装齐）",
        "brew install switchaudio-osx   # 可选：测试扬声器时切换默认输出设备",
    ],
    "Linux": [
        "sudo apt install -y ffmpeg alsa-utils v4l-utils   # 采集/播放/设备查询",
        "sudo apt install -y pulseaudio-utils              # 用 PulseAudio 按名字选设备（可选）",
    ],
}


# --------------------------------------------------------------------------
# 采集 / 播放后端
# --------------------------------------------------------------------------

def capture_video_frame(dev, out_png: Path, size: str, gray_raw: Path | None = None):
    """抓一帧。gray_raw 非空时同时输出原始灰度（用于清晰度计算）。"""
    if IS_MAC:
        idx = dev["index"] if isinstance(dev, dict) else dev
        base = ["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation",
                "-framerate", "30", "-video_size", size, "-i", f"{idx}:none"]
    elif IS_LINUX:
        node = dev["dev"] if isinstance(dev, dict) else dev
        base = ["ffmpeg", "-hide_banner", "-y", "-f", "v4l2",
                "-input_format", "mjpeg", "-video_size", size, "-i", node]
    else:
        return False, "unsupported platform", 0.0

    rc, _, err = run(base + ["-frames:v", "1", "-update", "1", str(out_png)], timeout=45)
    if rc != 0 or not out_png.exists() or out_png.stat().st_size < 1024:
        return False, (err.strip().splitlines() or ["capture failed"])[-1], 0.0

    score = 0.0
    if gray_raw is not None:
        # 用低分辨率抓灰度帧算清晰度：快且足够稳定
        w, h = 640, 480
        rc2, _, _ = run(base + ["-video_size", f"{w}x{h}", "-frames:v", "1",
                                "-pix_fmt", "gray", "-f", "rawvideo", str(gray_raw)],
                        timeout=45)
        if rc2 == 0 and gray_raw.exists():
            data = gray_raw.read_bytes()
            if len(data) >= w * h:
                score = laplacian_var(data[:w * h], w, h)
    return True, "", score


def capture_video_clip(dev, out_mp4: Path, size: str, seconds: int):
    """录一段视频并返回实测帧率信息。"""
    if IS_MAC:
        idx = dev["index"] if isinstance(dev, dict) else dev
        cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation",
               "-framerate", "30", "-video_size", size, "-i", f"{idx}:none",
               "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", str(out_mp4)]
    elif IS_LINUX:
        node = dev["dev"] if isinstance(dev, dict) else dev
        cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "v4l2", "-input_format", "mjpeg",
               "-video_size", size, "-i", node, "-t", str(seconds),
               "-c:v", "libx264", "-preset", "ultrafast", str(out_mp4)]
    else:
        return None, "unsupported platform"
    t0 = time.time()
    rc, _, err = run(cmd, timeout=seconds + 60)
    wall = time.time() - t0
    if rc != 0:
        return None, (err.strip().splitlines() or ["record failed"])[-1]
    m = re.findall(r"frame=\s*(\d+)", err)
    frames = int(m[-1]) if m else 0
    return {"frames": frames, "wall_s": round(wall, 1),
            "fps": round(frames / wall, 1) if wall > 0 else 0.0,
            "file": str(out_mp4)}, ""


def _record_with_terminate(cmd, seconds, slack=10):
    """给 parecord 这类没有时长参数的工具用：跑 seconds 秒后终止。"""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    time.sleep(seconds)
    p.terminate()
    try:
        out, err = p.communicate(timeout=slack)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    return 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def make_audio_target(item, keyword=None):
    """把设备表里的一项，归一化成三种后端都能用的目标描述。"""
    t = {"name": item.get("name"), "mac_index": None, "pulse": None, "plughw": None}
    if IS_MAC:
        t["mac_index"] = item.get("index")
    else:
        t["plughw"] = item.get("plughw")
        src, _ = match_device(parse_pulse("sources"), keyword)
        if src:
            t["pulse"] = src["name"]
    return t


def make_play_target(item, keyword=None):
    """播放目标：Linux 优先用 PulseAudio sink 名（可精确选设备），退回 ALSA plughw。"""
    t = {"name": item.get("name") if item else None, "pulse": None, "plughw": None}
    if IS_LINUX:
        t["plughw"] = item.get("plughw") if item else None
        sink, _ = match_device(parse_pulse("sinks"), keyword)
        if sink:
            t["pulse"] = sink["name"]
    return t


def capture_audio(target, out_wav: Path, seconds: int, rate: int = 16000, channels: int = 2):
    """录音到 WAV（16-bit）。target 由 make_audio_target() 产出。返回 (成功, 说明)。"""
    if IS_MAC and target.get("mac_index") is not None:
        cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation",
               "-i", f":{target['mac_index']}", "-t", str(seconds),
               "-ac", str(channels), "-ar", str(rate), str(out_wav)]
        rc, _, err = run(cmd, timeout=seconds + 30)
    elif IS_LINUX and target.get("pulse") and have("parecord"):
        cmd = ["parecord", f"--device={target['pulse']}", "--file-format=wav",
               f"--rate={rate}", f"--channels={channels}", str(out_wav)]
        rc, _, err = _record_with_terminate(cmd, seconds)
    elif IS_LINUX and target.get("plughw"):
        if have("arecord"):
            cmd = ["arecord", "-D", target["plughw"], "-f", "S16_LE", "-r", str(rate),
                   "-c", str(channels), "-d", str(seconds), str(out_wav)]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-y", "-f", "alsa", "-i", target["plughw"],
                   "-t", str(seconds), "-ac", str(channels), "-ar", str(rate), str(out_wav)]
        rc, _, err = run(cmd, timeout=seconds + 30)
    else:
        return False, "没有可用的采集后端（需 ffmpeg / arecord / parecord）"

    if rc != 0 or not out_wav.exists():
        return False, (err.strip().splitlines() or ["record failed"])[-1]
    try:
        with wave.open(str(out_wav), "rb") as w:
            if w.getnframes() < rate * seconds * 0.3:
                return False, f"时长不足（{w.getnframes()} 帧）—— 可能未授权麦克风"
    except Exception as e:                                    # noqa: BLE001
        return False, f"WAV 不可读：{e}"
    return True, ""


def play_wav(path: Path, target=None):
    """播放 WAV。target 由 make_play_target() 产出（macOS 可传 None）。"""
    target = target or {}
    if IS_MAC:
        if have("afplay"):
            rc, _, err = run(["afplay", str(path)], timeout=180)
            return rc == 0, err.strip()
        if have("ffmpeg"):
            rc, _, err = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                              "-i", str(path), "-f", "audiotoolbox", "-"], timeout=180)
            return rc == 0, err.strip()
        return False, "没有可用的播放器（afplay/ffmpeg）"
    if IS_LINUX:
        if target.get("pulse") and have("paplay"):
            rc, _, err = run(["paplay", f"--device={target['pulse']}", str(path)], timeout=180)
            if rc == 0:
                return True, ""
        if have("aplay"):
            rc, _, err = run(["aplay", "-D", target.get("plughw") or "default", str(path)],
                             timeout=180)
            return rc == 0, err.strip()
        if have("ffmpeg"):
            rc, _, err = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                              "-i", str(path), "-f", "alsa", "default"], timeout=180)
            return rc == 0, err.strip()
        return False, "没有可用的播放器（paplay/aplay/ffmpeg）"
    return False, "unsupported platform"


# --------------------------------------------------------------------------
# 测试音频生成（纯标准库）
# --------------------------------------------------------------------------

def _to_int16(x: float) -> int:
    v = int(max(-1.0, min(1.0, x)) * 32767)
    return v


def write_stereo_wav(path: Path, rate: int, chans: list[list[int]]):
    n = min(len(c) for c in chans) if chans else 0
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(chans))
        w.setsampwidth(2)
        w.setframerate(rate)
        buf = array.array("h")
        for i in range(n):
            for c in chans:
                buf.append(c[i])
        if sys.byteorder == "big":
            buf.byteswap()
        w.writeframes(buf.tobytes())


def gen_sine(path: Path, freq=1000.0, seconds=10.0, amp=0.5, rate=48000):
    n = int(rate * seconds)
    d = [_to_int16(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]
    write_stereo_wav(path, rate, [d, list(d)])
    return path


def gen_sweep(path: Path, f0=20.0, f1=20000.0, seconds=15.0, amp=0.5, rate=48000):
    """对数扫频（20Hz→20kHz），查频响断层与异响。"""
    n = int(rate * seconds)
    data = []
    phase = 0.0
    k = math.log(f1 / f0)
    for i in range(n):
        t = i / rate
        f = f0 * math.exp(k * t / seconds)
        phase += 2 * math.pi * f / rate
        data.append(_to_int16(amp * 0.6 * math.sin(phase)))
    write_stereo_wav(path, rate, [data, list(data)])
    return path


def gen_pink_noise(path: Path, seconds=10.0, amp=0.35, rate=48000):
    """Voss-McCartney 粉噪（比白噪更接近人耳感知的能量分布）。

    优化点：第 r 行只在 i % 2^r == 0 时更新，也就是 i 的末尾零个数 tz 决定
    本步要更新 0..tz 行（平均约 2 行/样本），并用增量维护总和 —— 否则 10s@48k
    要跑 770 万次内层循环，纯 Python 会卡好几秒。
    """
    rnd = random.Random(20261001)
    rows = 16
    n = int(rate * seconds)
    values = [rnd.uniform(-1, 1) for _ in range(rows)]
    total = sum(values)
    out = []
    scale = amp * 3.0 / rows
    for i in range(n):
        tz = (i & -i).bit_length() - 1
        for r in range(min(tz + 1, rows)):
            old = values[r]
            new = rnd.uniform(-1, 1)
            values[r] = new
            total += new - old
        out.append(_to_int16(scale * total))
    write_stereo_wav(path, rate, [out, list(out)])
    return path


def gen_channel_test(path: Path, seconds=9.0, rate=48000):
    """左 1kHz → 右 800Hz → 双声道，各 2.5s，间隔 0.5s 静音：验证双声道是否都通。"""
    seg = int(rate * 2.5)
    gap = [0] * int(rate * 0.5)
    left = [_to_int16(0.5 * math.sin(2 * math.pi * 1000 * i / rate)) for i in range(seg)]
    right = [_to_int16(0.5 * math.sin(2 * math.pi * 800 * i / rate)) for i in range(seg)]
    z = [0] * seg
    L = left + gap + z + gap + left
    R = z + gap + right + gap + left
    write_stereo_wav(path, rate, [L, R])
    return path


def gen_voice_wav(path: Path, text="乐可，我们来听写英语单词吧。请把书放到我前面。") -> Path | None:
    """中文播报样本：macOS 用 say，Linux 有 piper 就用 piper。用于测可懂度。

    macOS 上 say 直接出 AIFF（afplay 能播）；有 ffmpeg 才转成 WAV 统一格式。
    """
    if IS_MAC and have("say"):
        aiff = path.with_suffix(".aiff")
        rc, _, _ = run(["say", "-v", "Tingting", "-r", "180", "-o", str(aiff), text], timeout=60)
        if rc != 0 or not aiff.exists():
            return None
        if have("ffmpeg"):
            rc2, _, _ = run(["ffmpeg", "-hide_banner", "-y", "-loglevel", "error",
                             "-i", str(aiff), "-ar", "48000", "-ac", "2", str(path)], timeout=60)
            if rc2 == 0 and path.exists():
                aiff.unlink(missing_ok=True)
                return path
        return aiff            # 直接用 AIFF 播放，不依赖 ffmpeg
    if IS_LINUX and have("piper"):
        rc, _, _ = run(["piper", "--output_file", str(path)], timeout=60)
        return path if rc == 0 and path.exists() else None
    return None


# --------------------------------------------------------------------------
# 测试：摄像头
# --------------------------------------------------------------------------

def test_camera(ctx, args):
    head("① 摄像头验收（对应 06 文档到货验收第 ① 条）")
    if not have("ffmpeg"):
        print(warn("未安装 ffmpeg，无法采集视频"))
        print(info("安装：brew install ffmpeg（macOS） / sudo apt install ffmpeg（Linux）"))
        return {"pass": False, "reason": "no ffmpeg"}

    items = ctx["survey"]["video"]
    if not items:
        print(bad("没有发现任何视频采集设备"))
        return {"pass": False, "reason": "no video device"}

    dev, all_devs = match_device(items, args.device)
    print(info("候选视频设备："))
    for d in all_devs:
        mark = C(" ←选中", "g") if (dev and d is dev) else ""
        print(f"      {d.get('index', d.get('dev'))}: {d.get('name')}{mark}")
    if dev is None:
        print(bad("未找到绿联摄像头（关键词 UGREEN / 绿联）"))
        print(warn("确认设备已插好；若用的是内建相机，请显式指定 --device FaceTime"))
        return {"pass": False, "reason": "ugreen camera not found"}

    res = {"device": dev.get("name"), "resolutions": {}, "max_res": None, "focus": {}}
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    # --- 分辨率探测（判定 2K 款 65545 / 1080p 款 45644）---
    print(info("探测支持的分辨率（逐档抓 1 帧）…"))
    ok_sizes = []
    for size in RESOLUTIONS:
        png = outdir / f"cam_{size}.jpg"
        good, err, _ = capture_video_frame(dev, png, size)
        res["resolutions"][size] = good
        if good:
            ok_sizes.append(size)
            print(ok(f"{size} 可采集（{png.stat().st_size // 1024} KB）"))
        else:
            print(info(f"{size} 不支持（{err[:60]}）"))

    if not ok_sizes:
        print(bad("所有分辨率都采集失败 —— 检查摄像头权限或设备占用"))
        res["pass"] = False
        return res

    # 按像素数取最高，而不是「最后一个成功的」——探测顺序变了也不会误判
    res["ok_resolutions"] = ok_sizes
    res["max_res"] = max(ok_sizes, key=px_of)
    px = px_of(res["max_res"])
    if res["max_res"] in RES_2K or px > 1920 * 1080:
        print(ok(f"最高分辨率 {res['max_res']}（{px / 1e6:.1f} MP）"
                 f"→ 判定为 2K 款（65545）✓ 与订单一致"))
        res["model_ok"] = True
    else:
        print(warn(f"最高只到 {res['max_res']}（{px / 1e6:.1f} MP）"
                   f"→ 疑似定焦 1080p 款（45644），与订单不符！"))
        res["model_ok"] = False

    # --- 持续帧率（无需人工，先跑；非交互场景也能出结论）---
    print("\n" + info(f"录 {args.seconds}s 视频测持续帧率…"))
    clip = outdir / "cam_clip.mp4"
    r, err = capture_video_clip(dev, clip, res["max_res"], args.seconds)
    fps_ok = False
    if r:
        res["clip"] = r
        fps_ok = r["fps"] >= 15
        tag = ok if r["fps"] >= 25 else warn
        print(tag(f"实测 {r['frames']} 帧 / {r['wall_s']}s ≈ {r['fps']} fps（标称 30fps）"
                  f"{'' if fps_ok else ' —— 低于 15fps，跟随时会明显卡顿'}"))
    else:
        print(bad(f"录像失败：{err[:80]}"))

    # --- 自动对焦（要换两次场景，非交互时自动跳过）---
    focus_ok = None                      # None = 未测
    print("\n" + info("自动对焦测试（需要你换两次场景）："))
    raw = outdir / "cam_focus.raw"
    far_png = outdir / "cam_focus_far.jpg"
    near_png = outdir / "cam_focus_near.jpg"
    print(info("  第 1 张：镜头对着 1m 以外的场景，保持不动…"))
    print(C("      按回车继续", "y"), end=" ")
    try:
        input()
    except EOFError:
        print("(非交互模式 → 跳过对焦测试)")
        res["focus"] = {"skipped": True}
    else:
        good, err, score_far = capture_video_frame(dev, far_png, res["max_res"], raw)
        if not good:
            print(bad(f"抓帧失败：{err[:80]}"))
        else:
            print(info(f"  远景清晰度分数 = {score_far:.1f}（拉普拉斯方差，越大越清晰）"))
            print(info("  第 2 张：把绘本/文字卡片放到镜头前 30–50cm，让画面占满…"))
            print(C("      按回车继续", "y"), end=" ")
            try:
                input()
            except EOFError:
                print("(非交互模式 → 只保留远景)")
                res["focus"] = {"far": score_far, "skipped": True}
            else:
                good, err, score_near = capture_video_frame(dev, near_png, res["max_res"], raw)
                if not good:
                    print(bad(f"抓帧失败：{err[:80]}"))
                    res["focus"] = {"far": score_far}
                else:
                    ratio = (score_near / score_far) if score_far > 1 else 0.0
                    res["focus"] = {"far": score_far, "near": score_near,
                                    "ratio": round(ratio, 2)}
                    if score_near > 40 and ratio > 1.2:
                        focus_ok = True
                        print(ok(f"近拍清晰度 {score_near:.1f}，是远景的 {ratio:.2f} 倍 "
                                 f"→ 自动对焦工作正常 ✓"))
                    elif score_near > 40:
                        focus_ok = True
                        print(warn(f"近拍绝对清晰度 {score_near:.1f} 尚可，"
                                   f"但相对远景仅 {ratio:.2f} 倍"))
                        print(info("  可能是目标对比度不足；换一张文字密集的卡片再测一次"))
                    else:
                        focus_ok = False
                        print(bad(f"近拍清晰度仅 {score_near:.1f} → 疑似定焦或对焦失效"))
                        print(info(f"  人工复核：打开 {near_png} 看是否糊"))

    res["pass"] = bool(res.get("model_ok")) and fps_ok and (focus_ok is not False)
    return res


# --------------------------------------------------------------------------
# 测试：麦克风
# --------------------------------------------------------------------------

def _mic_capture(ctx, args, seconds, rate, channels, tag):
    dev = ctx["mic_dev"]
    out = Path(args.out) / f"mic_{tag}.wav"
    good, err = capture_audio(dev, out, seconds, rate, channels)
    if not good:
        print(bad(f"录音失败：{err[:120]}"))
        if IS_MAC:
            print(info("  macOS 需授权终端「麦克风」权限：系统设置 → 隐私与安全性 → 麦克风"))
        return None, None
    r, ch, chans = read_wav_mono(out)
    return out, {"rate": r, "channels": ch, "chans": chans}


def test_mic(ctx, args):
    head("② 麦克风验收（对应 06 文档到货验收第 ③ 条 + 3.4 现实约束）")
    if not have("ffmpeg") and not have("arecord") and not have("parecord"):
        print(warn("缺采集工具：brew install ffmpeg / sudo apt install alsa-utils"))
        return {"pass": False, "reason": "no capture tool"}

    items = ctx["survey"]["capture"]
    if not items:
        print(bad("没有发现任何音频采集设备"))
        return {"pass": False, "reason": "no capture device"}
    dev, all_devs = match_device(items, args.device)
    print(info("候选采集设备："))
    for d in all_devs:
        mark = C(" ←选中", "g") if (dev and d is dev) else ""
        print(f"      {d.get('index', d.get('name'))}: {d.get('name')}{mark}")
    if dev is None:
        print(bad("未找到绿联音频设备（UGREEN Camera Audio）"))
        return {"pass": False, "reason": "ugreen audio not found"}
    ctx["mic_dev"] = make_audio_target(dev, args.device)

    si = alsa_stream_info(dev.get("card")) if IS_LINUX else {}
    if si:
        cap = si.get("capture") or {}
        pb = si.get("playback") or {}
        print(info(f"USB 音频原生能力（只读 /proc/asound）："
                   f"采集 {cap.get('rates', '?')} Hz / {cap.get('channels', '?')} ch"
                   f"；播放 {pb.get('rates', '?')} Hz / {pb.get('channels', '?')} ch"))
        if str(cap.get("rates", "")).strip() in ("16000", "[16000]"):
            print(info("  采集原生固定 16 kHz —— 对 ASR 足够，但录不到宽带音频"))

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print("\n" + info(f"静音底噪测试：请保持安静，录 {args.seconds}s（双声道 16kHz）…"))
    time.sleep(0.5)
    path, meta = _mic_capture(ctx, args, args.seconds, 16000, 2, "quiet")
    if path is None:
        return {"pass": False, "reason": "capture failed"}
    res = {"file": str(path), "meta": {k: v for k, v in meta.items() if k != "chans"},
           "native": {}, "hw_params": si}
    sl = samples_stats(meta["chans"][0])
    print(info(f"  原生格式：{meta['rate']} Hz / {meta['channels']} ch"))
    for i, ch in enumerate(meta["chans"]):
        st = samples_stats(ch)
        res["native"][f"ch{i}"] = st
        print(info(f"  声道 {i}：RMS {st['rms_dbfs']} dBFS / 峰值 {st['peak_dbfs']} dBFS"))
    res["quiet_rms"] = sl["rms_dbfs"]
    res["all_zero"] = bool(sl["n"]) and sl["peak_dbfs"] < -90
    if res["all_zero"]:
        print(warn(f"录音全零（{sl['n']} 个采样逐位为 0）→ 采集流已卡死，不是「房间安静」"))
        print(info("  这种状态重开设备也不会恢复，必须复位 USB：拔插摄像头，或"))
        print(info("  sudo sh -c 'echo 3-1 > /sys/bus/usb/drivers/usb/unbind; "
                   "sleep 1; echo 3-1 > /sys/bus/usb/drivers/usb/bind'"))
    elif sl["rms_dbfs"] < -90:                  # 树莓派上最常见的坑：采集增益为 0 / 被静音
        print(warn(f"底噪仅 {sl['rms_dbfs']} dBFS ≈ 数字静音 → 麦克风可能被静音、未供电或通道选错"))
        print(info("  树莓派排查：alsamixer -c <card> → F4 选 Capture → ↑ 提增益 / 按 M 取消静音"))
        print(info("  或换个通道：--device 'plughw:1,0'"))

    if meta["channels"] >= 2:
        chans = meta["chans"]
        n = min(len(c) for c in chans)
        identical = all(chans[0][:n] == c[:n] for c in chans[1:])
        diff = abs(res["native"]["ch0"]["rms_dbfs"] - res["native"]["ch1"]["rms_dbfs"])
        res["ch_diff_db"] = round(diff, 1)
        res["chans_identical"] = identical
        if res.get("all_zero"):
            pass            # 全零时两声道当然逐位相同，上面的告警已经说明问题
        elif identical:
            print(warn("两声道采样逐位完全相同 → 设备上行只有一路信号"
                       "（一路复制，或双麦在硬件内混音）"))
            print(info("  含义：软件拿不到两路独立信号，做不了声源定位/波束成形；对 ASR 无影响"))
        elif diff > 12:
            print(warn(f"两声道电平差 {diff:.1f} dB —— 可能只有一路麦克风在工作"))
        else:
            print(ok(f"双声道均为独立信号（电平差 {diff:.1f} dB）✓"))

    # --- 说话测试 ---
    print("\n" + info("拾音测试：请用正常音量说「乐可，你好，今天天气不错」，录 6s…"))
    print(C("      按回车开始", "y"), end=" ")
    try:
        input()
    except EOFError:
        print("(非交互，跳过说话测试)")
        res["pass"] = True
        return res
    path2, meta2 = _mic_capture(ctx, args, 6, 16000, 2, "speech")
    if path2 is None:
        res["pass"] = False
        return res
    st = samples_stats(meta2["chans"][0])
    res["speech_rms"] = st["rms_dbfs"]
    res["speech_peak"] = st["peak_dbfs"]
    res["speech_over_quiet_db"] = round(st["rms_dbfs"] - sl["rms_dbfs"], 1)
    print(info(f"  说话段 RMS {st['rms_dbfs']} dBFS / 峰值 {st['peak_dbfs']} dBFS / "
               f"静音占比 {st['silence_ratio'] * 100:.0f}%"))
    print(info(f"  信噪比（说话 − 底噪）= {res['speech_over_quiet_db']} dB"))
    if st["rms_dbfs"] > -40 and res["speech_over_quiet_db"] > 10:
        print(ok("拾音正常：有效信号明显高于底噪 ✓"))
    elif st["clip_ratio"] > 0.001:
        print(warn(f"出现削波（{st['clip_ratio'] * 100:.2f}% 采样贴顶）→ 说明增益偏高，但麦克风是活的"))
    else:
        print(bad("说话段电平过低 → 检查是否对着麦克风、或麦克风未被系统选中"))
    print(info(f"产物：{path2}（可直接播放回听）"))

    if not args.speed_distance:
        res["pass"] = res.get("speech_over_quiet_db", 0) > 10 or st["clip_ratio"] > 0.001
        return res

    # --- 距离衰减（验证 06 文档「理想 0.5–3m」）---
    head("② b 拾音距离衰减（验证「理想 0.5–3m」这一说法）")
    print(info("每档录 4s，请用同一音量、正面朝向设备说话；测完会与 06 文档结论对比"))
    table = []
    for d in (0.5, 1.0, 2.0, 3.0):
        print(C(f"      请站到 {d} m，按回车开始（说「小马过河，一二三四五」）", "y"), end=" ")
        try:
            input()
        except EOFError:
            break
        p, m = _mic_capture(ctx, args, 4, 16000, 2, f"dist_{str(d).replace('.', '_')}m")
        if p is None:
            continue
        s = samples_stats(m["chans"][0])
        table.append((d, s["rms_dbfs"], s["peak_dbfs"]))
        print(info(f"      {d} m → RMS {s['rms_dbfs']} dBFS / 峰值 {s['peak_dbfs']} dBFS"))
    res["distance"] = table
    if len(table) >= 2:
        ref = table[0][1]
        print("\n" + info("衰减汇总（相对 0.5m）："))
        for d, rms, _ in table:
            print(f"      {d} m: {rms:>7.1f} dBFS   相对衰减 {rms - ref:>6.1f} dB")
        usable = [d for d, rms, _ in table if rms > -45]
        res["usable_range"] = max(usable) if usable else 0
        print(ok(f"本环境下可用拾音距离 ≈ {res['usable_range']} m")
              if res["usable_range"] >= 1 else warn("可用距离偏近，建议近讲使用"))
    res["pass"] = True
    return res


# --------------------------------------------------------------------------
# 测试：扬声器
# --------------------------------------------------------------------------

def test_speaker(ctx, args):
    head("③ 扬声器验收（对应 06 文档到货验收 + 3.4「约 1W，户外听不清」）")
    ready, missing = backend_ready("speaker")
    if not ready:
        print(bad(f"缺播放工具：{missing}"))
        return {"pass": False, "reason": missing}

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    res = {}

    # 输出设备提示
    if IS_MAC:
        sp = ctx["survey"].get("sp_audio", [])
        cur = next((d for d in sp if d.get("default_out")), None)
        print(info(f"当前默认输出：{cur['name'] if cur else '未知'}"))
        if cur and "ugreen" not in cur["name"].lower() and "绿联" not in cur:
            print(warn("默认输出不是绿联一体机 → afplay 会走系统默认输出"))
            print(info("  切法一：系统设置 → 声音 → 输出 → 选「UGREEN Camera Audio」"))
            print(info("  切法二：brew install switchaudio-osx 后 "
                       "SwitchAudioSource -s 'UGREEN Camera Audio'"))
        if not args.no_play:
            print(C("      准备好听声音后按回车开始（会有 1kHz 测试音）", "y"), end=" ")
            try:
                input()
            except EOFError:
                print("(非交互模式 → 只生成文件不播放)")
                args.no_play = True
    else:
        items = ctx["survey"]["playback"]
        dev, all_devs = match_device(items, args.device)
        print(info("候选播放设备："))
        for d in all_devs:
            mark = C(" ←选中", "g") if (dev and d is dev) else ""
            print(f"      {d.get('name')}{mark}")
        if dev is None:
            print(warn("未找到绿联播放设备 → 将走 ALSA `default`（可能出到 HDMI，听不到）"))
            print(info("  指定设备：--device camera   或   --device 'plughw:1,0'"))
        card = (dev or {}).get("card")
        vol, unmuted = alsa_playback_volume(card)
        if vol is not None:
            res["volume_pct"] = vol
            if vol == 0 or not unmuted:
                print(warn(f"ALSA 播放音量 {vol}%"
                           f"{'（已静音）' if not unmuted else ''}"
                           f" → 会「播放成功但完全没声」，正在自动修正…"))
                fixed, msg = fix_playback_volume(card)
                if fixed:
                    print(ok("已自动置为 100% 并取消静音 ✓"))
                    print(info("  重启后可能丢失 → 永久生效：sudo alsactl store"))
                    res["volume_pct"] = 100
                    res["volume_autofixed"] = True
                else:
                    print(bad(f"自动修正失败：{msg}"))
                    print(info(f"  手动执行：amixer -c {card} sset PCM 100%"))
            else:
                print(info(f"ALSA 播放音量 {vol}%"))
        ctx["spk_dev"] = make_play_target(dev, args.device)

    # --- 生成测试音 ---
    print("\n" + info("生成测试音频…"))
    files = {
        "1kHz 正弦（标称测试频点）": gen_sine(outdir / "spk_1khz.wav"),
        "对数扫频 20Hz→20kHz": gen_sweep(outdir / "spk_sweep.wav"),
        "粉噪声（主观音量感）": gen_pink_noise(outdir / "spk_pink.wav"),
        "左右声道分离": gen_channel_test(outdir / "spk_lr.wav"),
    }
    voice = gen_voice_wav(outdir / "spk_voice.wav")
    if voice:
        files["中文播报（可懂度）"] = voice
    for k, v in files.items():
        print(ok(f"{k} → {v.name}"))

    if args.no_play:
        print(warn("--no-play：仅生成文件，未播放"))
        res["files"] = {k: str(v) for k, v in files.items()}
        res["pass"] = True
        return res

    # --- 播放 ---
    print("\n" + info("播放（每段结束后有 1s 间隔）…"))
    dev = ctx.get("spk_dev")
    for k, v in files.items():
        t0 = time.time()
        good, err = play_wav(v, dev)
        dt = time.time() - t0
        if good:
            print(ok(f"{k} 已播放（{dt:.1f}s）"))
        else:
            print(bad(f"{k} 播放失败：{err[:100]}"))
        time.sleep(1.0)
        res.setdefault("played", {})[k] = good

    print("\n" + info("人工判定清单（这三条必须靠耳朵，脚本给不了结论）："))
    print("      1) 1kHz 是纯净的「滴——」，无破音/沙沙声 → 振膜与功放正常")
    print("      2) 扫频从低到高连续，无某段突然变小/消失 → 无频响断层")
    print("      3) 左右声道分离测试：先左、再右、再一起 → 应能听出位置变化（单喇叭则最后一段变响）")
    if voice:
        print("      4) 中文播报每个字都听得清 → 对儿童使用最重要的一条")

    if args.spl:
        head("③ b 声压测试（与 06 文档实验室数据对照）")
        print(info("用手机分贝计 App（如「Sound Meter」）放在设备正前方，逐档记录读数"))
        print(info("参考：绿联官方 1kHz 数据为 " +
                   " / ".join(f"{d} {v}dB" for d, v in SPL_REFERENCE)))
        print(info("播放 1kHz 测试音 10s，请在对应距离读数："))
        spl = []
        for dist, _ in SPL_REFERENCE:
            print(C(f"      请站到 {dist} 处，按回车播放 10s", "y"), end=" ")
            try:
                input()
            except EOFError:
                break
            play_wav(files["1kHz 正弦（标称测试频点）"], ctx.get("spk_dev"))
            raw = input("      手机读数（dB，直接回车跳过）：").strip()
            try:
                spl.append((dist, float(raw)))
            except ValueError:
                pass
        res["spl"] = spl
        if spl:
            print("\n" + info("实测 vs 官方："))
            ref = dict(SPL_REFERENCE)
            for d, v in spl:
                r = ref.get(d)
                delta = f"{v - r:+.0f} dB" if r else "—"
                print(f"      {d}: 实测 {v:.0f} dB   官方 {r if r else '—'}   差 {delta}")
            res["spl_ok"] = all(v >= (ref[d] - 6) for d, v in spl if d in ref)

    res["pass"] = all(res.get("played", {}).values())
    return res


# --------------------------------------------------------------------------
# deps / list / selftest
# --------------------------------------------------------------------------

def cmd_deps(ctx, args):
    s = ctx["survey"]
    head("依赖自检")
    print(info(f"平台：{s['platform']}   Python：{platform.python_version()}"))
    print("\n" + C("外部命令", "bold"))
    for t, present in s["tools"].items():
        if present:
            print(ok(t))
        else:
            print(info(f"{t}（缺失）"))
    missing_any = False
    for kind in ("camera", "mic", "speaker"):
        ready, miss = backend_ready(kind)
        if ready:
            print(ok(f"{kind} 后端就绪"))
        else:
            print(bad(f"{kind} 缺：{miss}"))
        missing_any = missing_any or not ready
    if missing_any:
        print("\n" + C("安装建议", "bold"))
        for line in INSTALL_HINT.get("macOS" if IS_MAC else "Linux", []):
            print(f"      {line}")

    head("设备发现")
    found = False
    if IS_LINUX:
        print(C("视频设备", "bold"))
        vids = s.get("video", []) or []
        real, plat = [], []
        for d in vids:                          # 区分「真采集口」与「ISP/解码等平台节点」
            nm = str(d.get("name", "")).lower()
            is_cam = any(k in nm for k in
                         ("usb-", "camera", "ugreen", "绿联", "webcam", "uvc"))
            (real if is_cam else plat).append(d)
        for d in real:
            hit = any(k in str(d.get("name", "")).lower() for k in ("ugreen", "绿联", "camera"))
            print((ok if hit else info)(f"{d.get('dev')}  {d.get('name')}"))
            found = found or hit
        if plat:
            devs = ", ".join(str(p.get("dev")) for p in plat[:8])
            print(info(f"另有 {len(plat)} 个平台内置视频节点（ISP/解码，非摄像头采集口）："
                       f"{devs}{' …' if len(plat) > 8 else ''}"))

        for label, key in (("采集设备", "capture"), ("播放设备", "playback")):
            print(C(label, "bold"))
            items = s.get(key) or []
            for d in items:
                hit = alsa_hit(d)
                print((ok if hit else info)(f"{d.get('name')}  [{d.get('plughw')}]"))
                found = found or hit
            if not items:                       # 空表 → 把原始输出摊开，便于定位
                print(info("（未解析到设备，原始输出如下）"))
                for ln in (s.get("alsa_raw", {}).get(key) or "（空）").splitlines()[:12]:
                    print(f"      {ln}")
        cards = s.get("alsa_cards") or ""
        if cards:
            print(C("/proc/asound/cards", "bold"))
            for ln in cards.splitlines()[:16]:
                print(f"      {ln}")

        # 一行可直接复制的目标设备，省得每次手敲 plughw
        cap = next((d for d in (s.get("capture") or []) if alsa_hit(d)), None)
        spk = next((d for d in (s.get("playback") or []) if alsa_hit(d)), None)
        if cap or spk:
            print(C("推荐目标", "bold"))
            if cap:
                print(info(f"采集  {cap['plughw']}   （{cap['card_name']} / {cap['dev_name']}）"))
            if spk:
                print(info(f"播放  {spk['plughw']}   （{spk['card_name']} / {spk['dev_name']}）"))
            if spk and spk.get("card") is not None and spk["card"] != 0:
                print(info(f"想让系统默认输出也走它：echo -e 'defaults.pcm.card {spk['card']}\\n"
                           f"defaults.ctl.card {spk['card']}' > ~/.asoundrc"))
    else:
        sp = s.get("sp_audio", [])
        print(C("系统音频", "bold"))
        for d in sp:
            tags = []
            if d.get("default_out"):
                tags.append("默认输出")
            if d.get("default_in"):
                tags.append("默认输入")
            hit = "ugreen" in d["name"].lower() or "绿联" in d["name"]
            print((ok if hit else info)(f"{d['name']}  {d.get('rate')}Hz  {'/'.join(tags)}"))
            found = found or hit
        print(C("系统摄像头", "bold"))
        for c in s.get("sp_camera", []):
            hit = "ugreen" in c["name"].lower()
            print((ok if hit else info)(f"{c['name']}  {c.get('model', '')}"))
            found = found or hit
        if s["video"]:
            print(C("avfoundation 索引（ffmpeg）", "bold"))
            for d in s["video"]:
                hit = any(k in d["name"].lower() for k in ("ugreen", "绿联"))
                print((ok if hit else info)(f"[{d['index']}] {d['name']}"))
            for d in s["audio"]:
                hit = any(k in d["name"].lower() for k in ("ugreen", "绿联"))
                print((ok if hit else info)(f"[{d['index']}] {d['name']}   (audio)"))
        elif s["notes"]:
            for n in s["notes"]:
                print(warn(n))

    print()
    if found:
        print(ok("发现绿联设备 ✓ 可以开始测试"))
    else:
        print(warn("未发现绿联设备 —— 请插好 USB 后重跑 `deps`"))
        print(info("macOS 首次使用还需授权终端「摄像头」与「麦克风」权限"))
    return {"pass": found, "deps_ok": not missing_any}


def cmd_list(ctx, args):
    return cmd_deps(ctx, args)


def cmd_selftest(ctx, args):
    """无硬件自检：验证波形生成、WAV 读写、电平与清晰度算法。"""
    head("自检（不需要硬件）")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    fails = []

    # 1) 正弦波的 RMS 应等于 amp/√2
    p = gen_sine(outdir / "_t_sine.wav", freq=1000, seconds=1.0, amp=0.5)
    r, ch, chans = read_wav_mono(p)
    st = samples_stats(chans[0])
    expect = 20 * math.log10(0.5 / math.sqrt(2))
    got = st["rms_dbfs"]
    if abs(got - expect) < 0.3:
        print(ok(f"正弦 RMS 校验：{got} dBFS ≈ 理论 {expect:.1f} dBFS"))
    else:
        print(bad(f"正弦 RMS 校验失败：{got} vs {expect:.1f}"))
        fails.append("sine_rms")

    # 2) 静音检测
    silent = array.array("h", [0] * 16000)
    s2 = samples_stats(silent)
    if s2["silence_ratio"] == 1.0 and s2["rms_dbfs"] < -100:
        print(ok("静音检测：全零输入 → 静音占比 100%"))
    else:
        print(bad(f"静音检测异常：{s2}"))
        fails.append("silence")

    # 3) 削波检测
    loud = array.array("h", [32767] * 1000 + [0] * 1000)
    s3 = samples_stats(loud)
    if s3["clip_ratio"] > 0.4:
        print(ok(f"削波检测：50% 贴顶输入 → clip_ratio {s3['clip_ratio']}"))
    else:
        print(bad(f"削波检测异常：{s3['clip_ratio']}"))
        fails.append("clip")

    # 4) 拉普拉斯方差：棋盘格 >> 纯色
    w = h = 64
    flat = bytes([128] * (w * h))
    checker = bytes([255 if (x + y) % 2 else 0 for y in range(h) for x in range(w)])
    v_flat, v_chk = laplacian_var(flat, w, h), laplacian_var(checker, w, h)
    if v_chk > v_flat * 100:
        print(ok(f"清晰度指标：棋盘格 {v_chk:.0f} ≫ 纯色 {v_flat:.0f}"))
    else:
        print(bad(f"清晰度指标异常：棋盘格 {v_chk:.0f} vs 纯色 {v_flat:.0f}"))
        fails.append("laplacian")

    # 5) 其它波形文件能生成
    for f in (gen_sweep(outdir / "_t_sweep.wav", seconds=2),
              gen_pink_noise(outdir / "_t_pink.wav", seconds=2),
              gen_channel_test(outdir / "_t_lr.wav", seconds=3)):
        if f.exists() and f.stat().st_size > 1024:
            print(ok(f"波形生成：{f.name}（{f.stat().st_size // 1024} KB）"))
        else:
            print(bad(f"波形生成失败：{f}"))
            fails.append(f.name)

    for f in outdir.glob("_t_*.wav"):
        f.unlink(missing_ok=True)
    print()
    print(ok("自检全部通过") if not fails else bad(f"自检失败项：{fails}"))
    return {"pass": not fails, "fails": fails}


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def load_results(outdir: Path) -> dict:
    """读取历史结果，让分开跑 camera/mic/speaker 也能累积成一份完整报告。"""
    p = outdir / "results.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            return {}
    return {}


def merge_results(outdir: Path, new: dict) -> dict:
    """合并进历史结果（最近一次为准），让分开跑也能累积成一份完整报告。"""
    outdir.mkdir(parents=True, exist_ok=True)
    allr = load_results(outdir)
    allr.update(new)
    (outdir / "results.json").write_text(
        json.dumps(allr, ensure_ascii=False, indent=2), encoding="utf-8")
    return allr


REASON_ZH = {
    "no ffmpeg": "缺 ffmpeg，未采集",
    "no capture tool": "缺采集工具（ffmpeg / arecord / parecord）",
    "no video device": "未发现视频设备",
    "no capture device": "未发现采集设备",
    "ugreen camera not found": "未发现绿联摄像头（设备没插？）",
    "ugreen audio not found": "未发现绿联音频设备（设备没插？）",
    "capture failed": "采集失败",
    "record failed": "录音失败",
}


def render_report(results: dict, args) -> Path:
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    results = merge_results(outdir, results)
    lines = [
        "# 绿联一体机验收报告",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> 执行平台：{'macOS' if IS_MAC else 'Linux'} / Python {platform.python_version()}  ",
        f"> 对应文档：06-第二批采购清单-音视频-v1.md「到货验收」、",
        ">   07-软件架构设计-v1.0-cursor-ac.md §3.4「一体机的现实约束」",
        "",
        "## 结论",
        "",
        "| 项 | 结果 | 关键数据 |",
        "|---|---|---|",
    ]

    cam = results.get("camera") or {}
    if "max_res" in cam:
        focus = cam.get("focus") or {}
        if "near" in focus:
            f_txt = (f"对焦 远{focus['far']:.0f} / 近{focus['near']:.0f}"
                     f"（{focus.get('ratio', 0)}×）")
        elif "far" in focus:
            f_txt = f"对焦 远{focus['far']:.0f} / 近未测"
        else:
            f_txt = "对焦未测（非交互）"
        clip = cam.get("clip") or {}
        fps_txt = f"{clip['fps']} fps" if clip else "帧率未测"
        n_ok = len(cam.get("ok_resolutions") or [])
        lines.append(f"| 摄像头 | {'✅' if cam.get('pass') else '⚠️'} | "
                     f"最高 {cam['max_res']}（{px_of(cam['max_res']) / 1e6:.1f} MP，"
                     f"支持 {n_ok}/{len(RESOLUTIONS)} 档）；"
                     f"{'2K 款 65545 与订单一致' if cam.get('model_ok') else '疑似 1080p 定焦 45644'}；"
                     f"{fps_txt}；{f_txt} |")
    else:
        lines.append(f"| 摄像头 | ⏭️ | "
                     f"{REASON_ZH.get(cam.get('reason'), cam.get('reason') or '未执行')} |")

    mic = results.get("mic") or {}
    if "quiet_rms" in mic:
        ch_txt = ("两声道逐位相同（单路信号）" if mic.get("chans_identical")
                  else f"双声道差 {mic.get('ch_diff_db', '—')} dB")
        if mic.get("all_zero"):
            ch_txt = "⚠ 录音全零（采集流卡死，需复位 USB）"
        si = mic.get("hw_params") or {}
        cap, pb = si.get("capture") or {}, si.get("playback") or {}
        hw_txt = (f"原生 采集{cap.get('rates', '?')}Hz/{cap.get('channels', '?')}ch、"
                  f"播放{pb.get('rates', '?')}Hz/{pb.get('channels', '?')}ch"
                  if (cap or pb) else "")
        lines.append(f"| 麦克风 | {'✅' if mic.get('pass') else '⚠️'} | "
                     f"底噪 {mic['quiet_rms']} dBFS；说话 {mic.get('speech_rms')} dBFS；"
                     f"信噪比 {mic.get('speech_over_quiet_db')} dB；{ch_txt}；{hw_txt}"
                     + (f"；可用距离 ≈ {mic['usable_range']} m" if mic.get("usable_range") else "") + " |")
    else:
        lines.append(f"| 麦克风 | ⏭️ | "
                     f"{REASON_ZH.get(mic.get('reason'), mic.get('reason') or '未执行')} |")

    spk = results.get("speaker") or {}
    if spk.get("played"):
        n_ok = sum(1 for v in spk["played"].values() if v)
        spl = spk.get("spl") or []
        spl_txt = "；".join(f"{d} {v:.0f}dB" for d, v in spl) if spl else "未测"
        vol_txt = ""
        if spk.get("volume_pct") is not None:
            vol_txt = f"音量 {spk['volume_pct']}%"
            vol_txt += "（脚本自动修正，原为 0%）" if spk.get("volume_autofixed") else ""
            vol_txt += "；"
        lines.append(f"| 扬声器 | {'✅' if spk.get('pass') else '⚠️'} | "
                     f"{vol_txt}{n_ok}/{len(spk['played'])} 段播放成功；声压实测 {spl_txt} |")
    else:
        lines.append(f"| 扬声器 | ⏭️ | "
                     f"{'仅生成文件未播放（--no-play）' if spk.get('files') else REASON_ZH.get(spk.get('reason'), spk.get('reason') or '未执行')} |")

    lines += ["", "## 与官方实验室数据的对照（1kHz 声压）", "",
              "| 距离 | 官方 | 实测 | 差 |", "|---|---|---|---|"]
    ref = dict(SPL_REFERENCE)
    for d, v in (spk.get("spl") or []):
        r = ref.get(d)
        lines.append(f"| {d} | {r if r else '—'} dB | {v:.0f} dB | "
                     f"{f'{v - r:+.0f} dB' if r else '—'} |")
    if not (spk.get("spl") or []):
        lines.append("| — | 118 / 86 / 76 dB | 未测 | — |")

    lines += ["", "## 产物清单", ""]
    for f in sorted(outdir.iterdir()):
        if f.is_file() and not f.name.startswith("_"):
            lines.append(f"- `{f.name}`  ({f.stat().st_size // 1024} KB)")
    lines += ["", "## 人工判定项（脚本给不出结论）", "",
              "- [ ] 1kHz 纯净无破音/沙沙声",
              "- [ ] 扫频连续无断层",
              "- [ ] 左右声道可分辨",
              "- [ ] 中文播报字字清晰（儿童使用第一要素）",
              "- [ ] 近拍 30–50cm 画面确实拉清晰（对比 cam_focus_near.jpg / cam_focus_far.jpg）",
              ""]

    rp = outdir / "report.md"
    rp.write_text("\n".join(lines), encoding="utf-8")
    return rp


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="绿联一体机（摄像头/麦克风/扬声器）验收测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[-1])
    ap.add_argument("action", choices=["deps", "list", "camera", "mic", "speaker",
                                       "selftest", "all"],
                    help="要执行的动作")
    ap.add_argument("--device", default=None, help="设备名关键词（默认自动匹配 UGREEN）")
    ap.add_argument("--seconds", type=int, default=5, help="录音/录像时长秒（默认 5）")
    ap.add_argument("--out", default="code/av_out", help="产物目录（默认 code/av_out）")
    ap.add_argument("--no-play", action="store_true", help="只生成音频不播放")
    ap.add_argument("--distance", dest="speed_distance", action="store_true",
                    help="mic 动作附加：拾音距离衰减测试")
    ap.add_argument("--spl", action="store_true", help="speaker 动作附加：声压测试")
    args = ap.parse_args(argv)
    # --out 默认按仓库布局解析成 <repo>/code/av_out；脚本被单独拷到别处
    # （例如树莓派上是 ~/04_av_test.py）时，退到「当前目录/av_out」，避免去写 /home/code。
    if not Path(args.out).is_absolute():
        root = Path(__file__).resolve().parent.parent
        args.out = str((root / args.out) if (root / "code").is_dir()
                       else (Path.cwd() / "av_out"))

    ctx = {"survey": survey()}
    results = {}

    dispatch = {"deps": cmd_deps, "list": cmd_list, "selftest": cmd_selftest,
                "camera": test_camera, "mic": test_mic, "speaker": test_speaker}

    if args.action == "all":
        for step in ("deps", "camera", "mic", "speaker"):
            if step == "mic":
                args.speed_distance = True
            fn = dispatch[step]
            key = "deps" if step in ("deps", "list") else step
            if step == "deps":
                results["deps"] = cmd_deps(ctx, args)
            else:
                results[key] = fn(ctx, args)
        rp = render_report(results, args)
        head("总结")
        for k, v in results.items():
            if k == "deps":
                continue
            flag = v.get("pass")
            print((ok if flag else warn)(f"{k}: {'通过' if flag else '需人工确认'}"))
        print()
        print(ok(f"报告已写入 {rp}"))
        return 0 if all(v.get("pass") for k, v in results.items() if k != "deps") else 1

    fn = dispatch[args.action]
    key = "deps" if args.action in ("deps", "list") else args.action
    results[key] = fn(ctx, args)
    if args.action in ("camera", "mic", "speaker"):
        rp = render_report(results, args)
        print()
        print(ok(f"报告已写入 {rp}"))
    return 0 if results[key].get("pass") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
