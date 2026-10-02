#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""05 语音控车 v0 —— F1.1 精确指令，本地闭环（不经 ECS/dsh）

对应 07-软件架构设计 §五：精确指令走本地解析（断网可用），dsh 慢通道留给抽象任务。
语音急停例外：行驶中后台持续监听，"停"字随时打断。

用法（按顺序递进）
------------------
  python3 05_voice_control_test.py prompts   # ① 生成应答语音（在 macOS 跑一次，产物自动同步）
  python3 05_voice_control_test.py check     # ② 自检：无麦克风验证模型+VAD+解析（用自带样本）
  python3 05_voice_control_test.py check ~/cmd_test/*.wav   # 用指定音频自检（可传多条）
  python3 05_voice_control_test.py parse "前进一米" "向后半米"  # 纯文本调词表（不加载模型）
  python3 05_voice_control_test.py calib     # ③ 校准编码器步/米（推车 1 米；没下地用内置推算值）
  python3 05_voice_control_test.py calib --default   # 把推算值落盘（可选，不写也一样能用）
  python3 05_voice_control_test.py drive     # ④ 无语音自测：里程闭环走 1 米（先跑这个！）
  python3 05_voice_control_test.py mic       # ⑤ 听一句 → 打印识别与解析（不动车）
  python3 05_voice_control_test.py loop      # ⑦ 主循环：唤醒词 → 听 → 解析 → 执行 → 播报
  python3 05_voice_control_test.py wake      # ⑧ 唤醒词自测（喊"小豆"应答，不动车）

依赖（唯一的 pip）
------------------
  # Debian 13 起系统 Python 拒绝 pip 装包（PEP 668 externally-managed），
  # 所以 sherpa-onnx 装在 venv 里；脚本会自动切过去，直接跑下面命令即可。
  python3 -m venv --system-site-packages ~/leko-venv   # apt 的 gpiozero/numpy 继续可见
  ~/leko-venv/bin/pip install sherpa-onnx
  其余依赖全来自 apt：gpiozero / numpy / alsa-utils（arecord/aplay/amixer）

模型（一次性，约 170MB）
----------------------
  R=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
  mkdir -p ~/models/asr && cd ~/models/asr
  curl -OL $R/silero_vad.onnx                       # VAD 断句
  curl -OL $R/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
  tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2 && rm *.tar.bz2
  # 唤醒词模型（注意 tag 是 kws-models，不是 asr-models）
  K=https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models
  curl -OL $K/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
  tar xf sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2 && rm sherpa-onnx-kws*.tar.bz2
  # 说明：此前的 zipformer-small-bilingual-zh-en-2023-02-16 已从官方仓库下架，
  # 现存的同名文件只有 streaming 版（与这里的非流式 API 不兼容），故改用 SenseVoice。
  # 想换更小的模型（Paraformer-zh-small，78MB）也能用，脚本按目录名自动选工厂方法。

v0 安全边界（没有雷达/悬崖/急停硬件时的跑法）
---------------------------------------------
  ① 速度 PWM 0.35（约 0.25m/s）；单条指令 ≤ 3m；倒车 ≤ 1m（前驱规则）
  ② 堵转 500ms 自动断电；语音"停"随时打断；板上 ON/OFF 开关 = 人工急停
  ③ 只在空旷平地跑，人跟在旁边
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

# ---------------------------------------------------------------- 运行环境自举
# sherpa-onnx 只装在 ~/leko-venv（系统 Python 受 PEP 668 保护，装不了 pip 包）。
# 用系统 python3 直接跑时，这里自动换成 venv 解释器重启自己，保证
#   python3 05_voice_control_test.py <子命令>
# 这个用法始终有效——不需要你记住 venv 路径。
VENV_PY = Path.home() / "leko-venv" / "bin" / "python3"


def _bootstrap():
    if sys.prefix != sys.base_prefix:            # 已经在 venv 里，直接继续
        return
    if not VENV_PY.exists():
        print(f"⚠ 没找到 {VENV_PY}，用系统 Python 继续（依赖靠 apt）")
        print("  装 sherpa-onnx：")
        print("    python3 -m venv --system-site-packages ~/leko-venv")
        print("    ~/leko-venv/bin/pip install sherpa-onnx")
        return
    # 管道/重定向时自动关缓冲：被 kill 掉也不丢日志（-u 不会随 execv 自动继承）
    argv = [str(VENV_PY)] + ([] if sys.stdout.isatty() else ["-u"])
    argv += [str(Path(__file__).resolve()), *sys.argv[1:]]
    os.execv(str(VENV_PY), argv)


_bootstrap()

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models" / "asr"
ASR_DIR = MODEL_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
VAD_MODEL = MODEL_DIR / "silero_vad.onnx"
PROMPT_DIR = BASE / "av_out" / "voice"
CALIB_FILE = BASE / "voice_calib.json"

# 引脚照抄 01~03（实车已校准：forward() = 车头即前驱驱动轮端前进）
PIN_L = dict(forward=23, backward=24, enable=12, pwm=True)
PIN_R = dict(forward=27, backward=17, enable=13, pwm=True)
ENC_L = dict(a=5, b=6)
ENC_R = dict(a=25, b=16)

SPEED = 0.35        # 行驶 PWM（≈0.25m/s，无雷达限速）
MAX_DIST = 3.0      # 单条指令最大距离（米）
MAX_REVERSE = 1.0   # 前驱：禁长倒
WHEEL_BASE = 0.17   # 两驱动轮中心距（米），calib 时尺量替换
STRAIGHT_K = 4.0    # 直线修正增益：右轮 PWM = SPEED + K×(左米-右米)
STALL_MS = 500      # PWM 有输出且编码器无脉冲 → 断电
TICK = 0.02         # 50Hz 控制周期
TURN_SIGN = +1      # 实车"左转"若向右转，改成 -1

# ---------------- 唤醒词（sherpa-onnx KeywordSpotter） ----------------
WAKE_WORDS = [("小豆", "xiao3 dou4"), ("小豆小豆", "xiao3 dou4 xiao3 dou4")]   # 豆=四声 dòu
KWS_DIR = MODEL_DIR / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
KWS_THRESHOLD = 0.25   # 越大越难触发（官方默认 0.25；喊不应就调小）
KWS_KEYWORDS_FILE = MODEL_DIR / "keywords.txt"    # make_keywords_file 自动生成
CONTINUE_WINDOW = 12.0  # 唤醒/指令后免唤醒续听窗口（秒）；行驶中不回睡眠（"停"要能打断）
PREROLL_S = 0.30       # 预缓冲：VAD 触发前 0.3s 拼回段首，补被削掉的首字

# 没下地时的推算步/米：520 电机 11PPR × 减速比 30 × 四倍频 = 1320 步/圈 ÷ 65mm 轮周长
SPM_EST = round(11 * 30 * 4 / (math.pi * 0.065))           # ≈ 6464，误差 ±20%
DEFAULT_CALIB = {"l_spm": SPM_EST, "r_spm": SPM_EST, "wheel_base": WHEEL_BASE}

PROMPT_TEXT = {
    "ok": "好嘞", "done": "到啦", "stop": "停",
    "miss": "没听清，请带上距离，比如前进一米", "stuck": "卡住了，检查一下",
}

JUNK_DBFS = -45.0    # 语音段 RMS 电平低于此 = 呼吸/远场杂音，不送识别（04_av 同思路）

PARTS_DIR = PROMPT_DIR / "parts"   # 拼接播报词段（prompts 子命令生成）
SPEAK_VERBS = ["前进", "后退", "左转", "右转", "掉头", "左前方前进", "右前方前进", "转个圈"]
SPEAK_CHARS = list("零一二三四五六七八九十点半百")
SPEAK_UNITS = ["米", "厘米", "公尺", "分米", "度"]
SPEAK_MISC = ["在", "完毕", "没找到指令", "已停止", "卡住了", "好嘞"]


# ---------------------------------------------------------------- 小工具

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        return 1, "", str(e)


def ask(prompt: str, hint: str = ""):
    """交互提问；非终端直接退出（input() 在 ssh/管道下会卡死，不抛异常）。"""
    if not sys.stdin.isatty():
        sys.exit("✗ 需要交互终端" + (f"；{hint}" if hint else ""))
    return input(prompt)


_PLAY_DEV = None


def playback_dev() -> str:
    """播放设备：直连绿联声卡 plughw。默认路由下 alay 会 audio open error 524
    （PipeWire 占着默认设备），ffplay 走 PipeWire 反而正常——所以绕开默认路由。"""
    global _PLAY_DEV
    if _PLAY_DEV is None:
        _, out, _ = run(["aplay", "-l"])
        m = re.search(r"card (\d+)[^\n]*(?:ugreen|绿联|camera)", out, re.I)
        _PLAY_DEV = f"plughw:{m.group(1)},0" if m else "default"
    return _PLAY_DEV


def _aplay(wav):
    """aplay 直连绿联卡播放；失败把报错打出来（别再静默吞掉）。"""
    rc, _, err = run(["aplay", "-q", "-D", playback_dev(), str(wav)])
    if rc != 0:
        print(f"   ⚠ 播放失败（{playback_dev()}）：{err.strip()[:100]}")


def play(name: str):
    """播 av_out/voice/<name>.wav；没有就打字幕（不因缺音频卡功能）。"""
    wav = PROMPT_DIR / f"{name}.wav"
    txt = PROMPT_TEXT.get(name, "")
    if not wav.exists():
        print(f"   🔊（无音频）{txt}")
        return
    if sys.platform == "darwin":
        run(["afplay", str(wav)])
    else:
        _aplay(wav)


def speak_seq(parts, gap=0.06):
    """按序拼接播放词段 wav（离线零依赖）；缺词段打字幕，不卡流程。"""
    miss = []
    for p in parts:
        wav = PARTS_DIR / f"{p}.wav"
        if wav.exists():
            if sys.platform == "darwin":
                run(["afplay", str(wav)])
            else:
                _aplay(wav)
            time.sleep(gap)
        else:
            miss.append(p)
    if miss:
        print(f"   🔊（缺词段 {'/'.join(miss)}：在 macOS 跑 prompts 补）")
    print(f"   🔊 {' '.join(parts)}")


def num2cn(n: int):
    """0~999 → 中文数字单字列表（45 → ['四','十','五']）。"""
    d = "零一二三四五六七八九"
    if n == 0:
        return ["零"]
    out = []
    if n >= 100:
        out += [d[n // 100], "百"]
        n %= 100
    if n >= 20:
        out += [d[n // 10], "十"]
        n %= 10
    elif n >= 10:
        out += ["十"]
        n %= 10
    if n:
        out.append(d[n])
    return out


def phrase_words(s_: str):
    """'一米'→[一,米]；'2.5米'→[二,点,五,米]；'1m'→[一,米]；'3cm'→[三,厘米]。"""
    s_ = (s_.replace("cm", "厘米").replace("CM", "厘米")
             .replace("dm", "分米").replace("DM", "分米"))
    s_ = re.sub(r"[mM]", "米", s_)
    words, i = [], 0
    while i < len(s_):
        for u in ("厘米", "公尺", "分米"):
            if s_.startswith(u, i):
                words.append(u)
                i += len(u)
                break
        else:
            ch = s_[i]
            if ch.isdigit() or ch == ".":
                j = i
                while j < len(s_) and (s_[j].isdigit() or s_[j] == "."):
                    j += 1
                for piece in re.findall(r"\d+|\.", s_[i:j]):
                    words.append("点") if piece == "." else words.extend(num2cn(int(piece)))
                i = j
            else:
                words.append(ch)
                i += 1
    return words


def announce_cmd(cmd: dict):
    """执行成功后的语音通报（需求 3：指令边界明确，执行完播报结果）。"""
    op = cmd.get("op")
    if op == "dist":
        verb = "后退" if cmd["reverse"] else "前进"
        d = "1米" if cmd.get("capped") else (cmd.get("dtxt") or f"{cmd['dist']}米")
        speak_seq([verb, *phrase_words(d), "完毕"])
    elif op == "arc":
        d = "左前方前进" if cmd["deg"] < 0 else "右前方前进"
        speak_seq([d, *phrase_words(cmd.get("dtxt") or f"{cmd['dist']}米"), "完毕"])
    elif op == "turn":
        deg = abs(cmd["deg"])
        if deg >= 360:
            speak_seq(["转个圈", "完毕"])
        elif deg >= 180:
            speak_seq(["掉头", "完毕"])
        else:
            w = "右转" if cmd["deg"] > 0 else "左转"
            speak_seq([w, *phrase_words(cmd.get("atxt") or f"{deg}度"), "完毕"])


def alsa_card(subcmd=("aplay", "-l")):
    _, out, _ = run(list(subcmd))
    m = re.search(r"card (\d+)[^\n]*(?:ugreen|绿联|camera)", out, re.I)
    return int(m.group(1)) if m else None


def ensure_volume():
    """绿联声卡 PCM 默认 0%（04_volume_ctl 的发现），best-effort 设 90%。"""
    if sys.platform == "darwin":
        return
    card = alsa_card()
    if card is not None:
        rc, _, _ = run(["amixer", "-c", str(card), "set", "PCM", "100%", "unmute"])
        if rc == 0:
            print(f"   🔊 声卡 {card} PCM → 100%（还嫌小是 1W 喇叭的物理极限，户外等 ReSpeaker+3W）")


def capture_dev():
    _, out, _ = run(["arecord", "-l"])
    m = re.search(r"card (\d+)[^\n]*(?:ugreen|绿联|camera)", out, re.I)
    return f"plughw:{m.group(1)},0" if m else "default"


def load_calib():
    """有 voice_calib.json 就用它，否则用推算默认值（不影响安全，只是走得准不准）。"""
    try:
        d = json.loads(CALIB_FILE.read_text())
        if d.get("l_spm") and d.get("r_spm"):
            return {**DEFAULT_CALIB, **d}
    except Exception:                                          # noqa: BLE001
        pass
    print(f"   ⚠ 用推算默认值 {SPM_EST} 步/米（下地后跑 calib 校准）")
    return dict(DEFAULT_CALIB)


# ---------------------------------------------------------------- 中文数字与指令解析（纯函数）

CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
      "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn2num(s: str):
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s == "半":
        return 0.5
    if "点" in s:                       # 一点五 / 1点5
        a, _, b = s.partition("点")
        ip = cn2num(a) if a else 0
        if ip is None or not b:
            return None
        bd = b if b.isdigit() else str(cn2num(b) or "")
        try:
            return float(ip) + float("0." + bd) if bd else None
        except ValueError:
            return None
    total, num = 0, 0
    for ch in s:
        if ch in CN:
            num = CN[ch]
        elif ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            return None
    return total + num


def _dist_match(text: str):
    """距离 → (米, 命中原文)；无距离 → (None, None)。"""
    m = re.search(r"([\d.]+|[零一二三四五六七八九十百两半]+(?:点[零一二三四五\d]+)?)"
                  r"(米|公尺|m|厘米|公分|cm|分米|dm)", text)
    if not m:
        return None, None
    v = cn2num(m.group(1))
    if v is None:
        return None, None
    unit = m.group(2)
    if unit in ("厘米", "公分", "cm"):
        v /= 100.0
    elif unit in ("分米", "dm"):
        v /= 10.0
    return round(min(v, MAX_DIST), 2), m.group(0)


# 前进动词：先列 ASR 常见两字谐音（前近/全近/平进…），再兜底单字。
# 单字（进/近/走/前/径）很宽，安全性靠两条：无距离不动 + 剩字≤3（_leftover_ok）。
VERB_FWD = "前进|往前|向前|直行|直走|前近|全近|全进|平进|平近|走|前|进|近|径"
VERB_BACK = "后退|后腿|倒退|倒车|往后|向后|退|倒"

FILLER = "啊呃嗯哦呀哎的地得吧了呐哈那这呢个呗"


def _leftover_ok(t: str, *parts) -> bool:
    """把指令成分从原句剥掉，剩字（豁免常见语气词）≤3 才算一条干净指令。

    防"今天走了五米路"这类闲聊误触发；"呃前进一米吧""小豆前进一米"照常通过。
    """
    rest = t
    for p in parts:
        if p:
            rest = rest.replace(p, "", 1)
    return len(re.sub(f"[{FILLER}]", "", rest)) <= 2


def parse(text: str):
    """识别文本 → 指令 dict；None = 无关话/闲聊句。

    "停"无条件最高优先（安全红线），不受任何句子检查限制。
    其余指令必须"有距离/角度 + 剩字≤3"双保险，防误触发。
    """
    if not text:
        return None
    t = re.sub(r"[\s，。,!？！？、'\"']", "", text.lower()).replace("１", "1")
    if re.search(r"停|刹车|别动|stop", t):
        return {"op": "stop"}
    dist, dtxt = _dist_match(t)
    ang, atxt = None, None
    a = re.search(r"([零一二三四五六七八九十百两\d]+)度", t)
    if a:
        ang = cn2num(a.group(1))
        atxt = a.group(0)
    # 斜向（左前方等）→ 定曲率弧线
    for w, deg in (("左后方", -135), ("右后方", 135), ("左前方", -45), ("右前方", 45)):
        if w in t:
            vm = re.search(VERB_FWD, t)
            if not dist:
                return {"op": "miss"} if _leftover_ok(t, w, vm.group(0) if vm else "") else None
            return {"op": "arc", "deg": deg, "dist": dist, "dtxt": dtxt} \
                if _leftover_ok(t, w, dtxt, vm.group(0) if vm else "") else None
    m2 = re.search(r"掉头|转身|转个圈", t)
    if m2:
        deg = 360 if "圈" in m2.group(0) else 180
        return {"op": "turn", "deg": deg} if _leftover_ok(t, m2.group(0)) else None
    if "左转" in t or "右转" in t:
        deg = abs(ang) if ang else 90
        w = "右转" if "右转" in t else "左转"
        return {"op": "turn", "deg": deg if w == "右转" else -deg, "atxt": atxt} \
            if _leftover_ok(t, w, atxt) else None
    vb = re.search(VERB_BACK, t)                  # 倒车先判（前驱：限 1 米）
    if vb:
        if dist:
            return {"op": "dist", "dist": min(dist, MAX_REVERSE), "reverse": True,
                    "capped": dist > MAX_REVERSE, "dtxt": dtxt} \
                if _leftover_ok(t, vb.group(0), dtxt) else None
        return {"op": "miss"} if _leftover_ok(t, vb.group(0)) else None
    vf = re.search(VERB_FWD, t)                   # 前进
    if vf:
        if not dist:
            return {"op": "miss"} if _leftover_ok(t, vf.group(0)) else None
        return {"op": "dist", "dist": dist, "reverse": False, "dtxt": dtxt} \
            if _leftover_ok(t, vf.group(0), dtxt) else None
    return None


# ---------------------------------------------------------------- ASR + VAD（sherpa-onnx）

def find_asr_dir() -> Path:
    """定位模型目录：优先 ASR_DIR，否则在 models/asr 下找唯一含 tokens.txt 的子目录。"""
    if ASR_DIR.exists():
        return ASR_DIR
    cands = [d for d in MODEL_DIR.glob("*")
             if d.is_dir() and next(d.glob("tokens.txt"), None)]
    if len(cands) == 1:
        print(f"   ℹ 使用模型目录：{cands[0].name}")
        return cands[0]
    sys.exit(f"✗ 模型未下载或目录不唯一：{MODEL_DIR}\n  下载命令见本文件头部注释")


def load_asr():
    """按模型目录内容自动选工厂方法：transducer / sense_voice / paraformer。

    注意：sherpa-onnx 1.13 起 `from_zipformer2` 已改名 `from_transducer`，
    且 streaming 模型不能用在非流式 API 上。
    """
    d = find_asr_dir()
    import sherpa_onnx
    tok = sorted(d.glob("tokens.txt"))
    enc = sorted(d.glob("encoder*.onnx"))
    dec = sorted(d.glob("decoder*.onnx"))
    joi = sorted(d.glob("joiner*.onnx"))
    par = sorted(d.glob("model*.onnx"))
    name = d.name.lower()
    if not tok:
        sys.exit(f"✗ {d} 里没有 tokens.txt")

    if enc and dec and joi:                       # 非流式 transducer（zipformer 等）
        print("   ℹ 模型类型：transducer")
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(enc[0]), decoder=str(dec[0]), joiner=str(joi[0]),
            tokens=str(tok[0]), num_threads=2,
            decoding_method="greedy_search")
    if par and "sense" in name:                   # SenseVoice（中英日韩粤）
        print("   ℹ 模型类型：sense_voice（int8，中英日韩粤）")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(par[0]), tokens=str(tok[0]), num_threads=2,
            language="zh", use_itn=True)
    if par and "paraformer" in name:
        print("   ℹ 模型类型：paraformer")
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(par[0]), tokens=str(tok[0]), num_threads=2)
    if par:                                       # zipformer-ctc 一类：单 model.onnx
        print("   ℹ 模型类型：zipformer_ctc（按单模型文件推断）")
        return sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=str(par[0]), tokens=str(tok[0]), num_threads=2)
    sys.exit(f"✗ {d} 里找不到可识别的模型组合：\n"
             f"   encoder/decoder/joiner 或 model.onnx（+ tokens.txt）")


def load_vad():
    if not VAD_MODEL.exists():
        sys.exit(f"✗ VAD 模型未下载：{VAD_MODEL}")
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = 0.4   # 0.5 触发晚，会削掉首字（前进→全近/平进的主因）
    cfg.silero_vad.min_silence_duration = 0.6   # 静音 0.6s 断句
    cfg.silero_vad.min_speech_duration = 0.25
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30), \
        cfg.silero_vad.window_size


def recognize(rec, samples) -> str:
    stream = rec.create_stream()
    stream.accept_waveform(16000, samples)
    # 1.13 起 OfflineRecognizer.decode() 改名为 decode_stream()（旧版兼容）
    decode = getattr(rec, "decode_stream", None) or rec.decode
    decode(stream)
    text = stream.result.text.strip()
    # SenseVoice 会把语言/情绪标签一起吐出来（<|zh|><|NEUTRAL|><|Speech|>），去掉
    return re.sub(r"<\|[^|]*\|>", "", text).strip()


def seg_dbfs(samples) -> float:
    """语音段 RMS 电平（dBFS）。samples 可能是 int16 刻度或归一化刻度，按峰值自适应。"""
    import numpy as np
    a = np.abs(np.asarray(samples, dtype=np.float32))
    if a.size == 0:
        return -99.0
    scale = 32768.0 if float(a.max()) > 8.0 else 1.0
    rms = float(np.sqrt(np.mean(np.square(a / scale))))
    return 20 * math.log10(rms) if rms > 0 else -99.0


# ---------------------------------------------------------------- 唤醒词 KWS

_PINYIN_INITIALS = ["zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l",
                   "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w"]
_MARKS = {"a": "āáǎà", "o": "ōóǒò", "e": "ēéěè",
          "i": "īíǐì", "u": "ūúǔù", "v": "ǖǘǚǜ"}


def _tone_mark(final: str, tone: int) -> str:
    """'iao',3 → 'iǎo'（a 优先；iu 标 u、ui 标 i）。"""
    if not 1 <= tone <= 4:
        return final
    for v in ("a", "o", "e"):
        i = final.find(v)
        if i >= 0:
            return final[:i] + _MARKS[v][tone - 1] + final[i + 1:]
    if final.endswith("iu"):
        i = final.find("u")
    elif "ui" in final:
        i = final.find("i")
    else:
        i = next((k for k, c in enumerate(final) if c in "iuv"), 0)
    return final[:i] + _MARKS[final[i]][tone - 1] + final[i + 1:]


def _phones(syl: str):
    """'xiao3' → ['x', 'iǎo']：声母 + 带调号韵母（与 KWS tokens.txt 对齐）。"""
    m = re.fullmatch(r"([a-z]+)([1-5])", syl.strip().lower())
    if not m:
        return None
    base, tone = m.group(1), int(m.group(2))
    for ini in _PINYIN_INITIALS:                    # zh/ch/sh 排在 z/c/s 前防误切
        if base.startswith(ini) and base[len(ini):]:
            return [ini, _tone_mark(base[len(ini):], tone)]
    return [_tone_mark(base, tone)]                # 零声母


def make_keywords_file() -> Path:
    """WAKE_WORDS → KWS 关键词文件；拼音逐字对照 tokens.txt，拆不开就报错退出。"""
    tok = KWS_DIR / "tokens.txt"
    if not tok.exists():
        sys.exit(f"✗ 缺 {tok}")
    tokens = {ln.split()[0] for ln in tok.read_text().splitlines()
              if ln.strip() and not ln.startswith("<")}
    lines = []
    for text, py in WAKE_WORDS:
        phones = []
        for syl in py.split():
            ph = _phones(syl)
            if not ph or not all(q in tokens for q in ph):
                sys.exit(f"✗ 拼音 {syl} → {ph} 与 tokens.txt 不齐（查 _phones/WAKE_WORDS）")
            phones += ph
        lines.append(" ".join(phones) + " @" + text)
    KWS_KEYWORDS_FILE.write_text("\n".join(lines) + "\n")
    print(f"   🎣 唤醒词：{' / '.join(t for t, _ in WAKE_WORDS)} → {KWS_KEYWORDS_FILE.name}")
    return KWS_KEYWORDS_FILE


def load_kws():
    if not KWS_DIR.exists():
        sys.exit(f"✗ 唤醒词模型未下载：{KWS_DIR}\n  下载命令见本文件头部注释")
    import sherpa_onnx

    def pick(pattern):
        files = sorted(KWS_DIR.glob(pattern))
        i8 = [f for f in files if "int8" in f.name]
        f = (i8 or files)[0] if (i8 or files) else None
        if not f:
            sys.exit(f"✗ {KWS_DIR} 缺 {pattern}")
        return f

    kws = sherpa_onnx.KeywordSpotter(
        tokens=str(KWS_DIR / "tokens.txt"),
        encoder=str(pick("encoder*chunk-16*.onnx")),
        decoder=str(pick("decoder*chunk-16*.onnx")),
        joiner=str(pick("joiner*chunk-16*.onnx")),
        keywords_file=str(make_keywords_file()),
        keywords_score=1.0,
        keywords_threshold=KWS_THRESHOLD,
        num_trailing_blanks=2,
        num_threads=2)
    print(f"   🎣 KWS 就绪（阈值 {KWS_THRESHOLD}，喊不应就调小）")
    return kws


def kws_feed(kws, stream, samples):
    """喂一小段音频给 KWS，命中返回唤醒词文本，否则 None。

    ⚠ KWS 的 accept_waveform 只认 float32 归一化刻度（int16 会被当
    原始字节读成垃圾 → 永不触发；VAD 没这问题，别弄混）。
    """
    import numpy as np
    if getattr(samples, "dtype", None) == np.int16:
        samples = samples.astype(np.float32) / 32768.0
    stream.accept_waveform(16000, samples)
    while kws.is_ready(stream):
        kws.decode_stream(stream)
    r = kws.get_result(stream)
    if not r:
        return None
    kw = getattr(r, "keyword", None) or (r if isinstance(r, str) else "")
    if kw and hasattr(kws, "reset_stream"):
        try:
            kws.reset_stream(stream)
        except TypeError:
            kws.reset_stream()
        except Exception:                           # noqa: BLE001
            pass
    return kw or None


class Listener(threading.Thread):
    """后台：唤醒词（KWS）→ 指令会话（VAD+ASR）→ 指令队列。

    状态机（把杂音挡在 ASR 之外，也给了指令明确边界）：
      WAKE  只跑唤醒词（聊天/电视声不进 ASR）
             ↓ 喊"小豆/小豆小豆" → 应答"在"
      ARMED VAD+ASR 收指令（0.3s 预缓冲拼回段首，补 VAD 削掉的首字）
             免唤醒续听 CONTINUE_WINDOW 秒；行驶中不回睡眠（"停"随时可打断）
             ↓ 超时无指令 → 回 WAKE
    """

    def __init__(self, cmd_q: queue.Queue, moving: threading.Event, enable_kws=True):
        super().__init__(daemon=True)
        self.q = cmd_q
        self.moving = moving
        self.rec = load_asr()
        self.vad, self.window = load_vad()
        self.kws = self.stream = None
        if enable_kws:
            self.kws = load_kws()
            self.stream = self.kws.create_stream()
        self.armed = not enable_kws        # 关 KWS（mic 自测）= 一直 ARMED
        self.last_act = time.monotonic()
        self.preroll = deque(maxlen=max(1, int(16000 * PREROLL_S / self.window)))

    def _mic(self):
        dev = capture_dev()
        print(f"   🎤 采集设备：{dev}")
        return subprocess.Popen(
            ["arecord", "-D", dev, "-q", "-t", "raw", "-f", "S16_LE",
             "-r", "16000", "-c", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=-1)

    def _with_preroll(self, seg):
        """VAD 段前面拼 0.3s 预缓冲：VAD 触发晚削掉的首字从这里找回来。"""
        import numpy as np
        peak = float(np.abs(seg).max()) if seg.size else 0.0
        pre = np.concatenate(list(self.preroll)) if self.preroll \
            else np.zeros(0, dtype=np.int16)
        if peak > 8.0:                      # int16 刻度
            return np.concatenate([pre, seg.astype(np.int16)])
        return np.concatenate([pre.astype(np.float32) / 32768.0,
                               seg.astype(np.float32)])

    def run(self):
        import numpy as np
        proc = self._mic()
        chunk = self.window * 2                       # int16 → 字节数
        while True:
            try:
                data = proc.stdout.read(chunk)
                if not data:
                    proc = self._mic()
                    continue
                samples = np.frombuffer(data, dtype=np.int16)
                self.preroll.append(samples)
                if not self.armed:                    # ---- WAKE：只跑唤醒词 ----
                    kw = kws_feed(self.kws, self.stream, samples)
                    if kw:
                        print(f"   🐟 唤醒：\"{kw}\"")
                        speak_seq(["在"])
                        self.armed = True
                        self.last_act = time.monotonic()
                    continue
                # ---- ARMED：VAD + ASR 收指令 ----
                self.vad.accept_waveform(samples)
                while not self.vad.empty():
                    seg = np.asarray(self.vad.front.samples)
                    self.vad.pop()
                    if len(seg) < 16000 * 0.25:       # 下限保留：单字"停"也要能过
                        continue
                    audio = self._with_preroll(seg)   # 预缓冲补首字
                    if seg_dbfs(audio) < JUNK_DBFS:   # 呼吸/远场杂音：不浪费识别
                        continue
                    text = recognize(self.rec, audio)
                    if not text:
                        continue
                    cmd = parse(text)
                    if cmd is None and len(text) <= 2:  # "嗯/啊"短杂音：不刷屏
                        continue
                    print(f"   🗣 \"{text}\" → {cmd if cmd else '（无关，忽略）'}")
                    if cmd:
                        self.q.put(cmd)
                        self.last_act = time.monotonic()
                if (not self.moving.is_set()
                        and time.monotonic() - self.last_act > CONTINUE_WINDOW):
                    self.armed = False
                    print("   💤 超时无指令，回到待唤醒（喊\"小豆\"）")
            except Exception as e:                     # noqa: BLE001
                print(f"   ⚠ 监听异常：{e}（重启麦克风）")
                time.sleep(1)
                try:
                    proc.kill()
                except Exception:                     # noqa: BLE001
                    pass
                proc = self._mic()


# ---------------------------------------------------------------- 驱动（gpiozero，里程闭环）

class Driver:
    def __init__(self, moving=None):
        from gpiozero import Motor, RotaryEncoder
        self.m_l = Motor(**PIN_L)
        self.m_r = Motor(**PIN_R)
        self.e_l = RotaryEncoder(**ENC_L, max_steps=0)
        self.e_r = RotaryEncoder(**ENC_R, max_steps=0)
        c = load_calib()
        self.l_spm, self.r_spm = c["l_spm"], c["r_spm"]
        self.wheel_base = c["wheel_base"]
        self.moving = moving or threading.Event()   # 行驶中=True：监听保持 ARMED（"停"能打断）
        print(f"   🛞 {self.l_spm:.0f}/{self.r_spm:.0f} 步/米，轮距 {self.wheel_base} m")

    def _stop(self):
        self.m_l.stop()
        self.m_r.stop()

    def _drive(self, sl, sr):
        """sl/sr：左右轮带符号 PWM（正=前进）。"""
        if sl >= 0:
            self.m_l.forward(sl)
        else:
            self.m_l.backward(-sl)
        if sr >= 0:
            self.m_r.forward(sr)
        else:
            self.m_r.backward(-sr)

    def _run(self, tl_m, tr_m, cmd_q: queue.Queue, speed=SPEED):
        """左右轮目标里程（米，带符号）→ 50Hz 闭环：目标达成/堵转/语音急停。"""
        tl, tr = tl_m * self.l_spm, tr_m * self.r_spm
        at_l, at_r = abs(tl), abs(tr)
        if at_l < 1 and at_r < 1:
            return "noop"
        self.e_l.steps = self.e_r.steps = 0
        self.moving.set()
        last = (self.e_l.steps, self.e_r.steps)
        last_t = time.monotonic()
        pl = pr = speed
        try:
            while True:
                # ① 语音急停（唯一可打断项）
                while not cmd_q.empty():
                    c = cmd_q.get_nowait()
                    if c.get("op") == "stop":
                        self._stop()
                        speak_seq(["已停止"])
                        print("   ⏹ 语音急停")
                        return "stop"
                    # 行驶中忽略其它指令（只许停）
                # ② 目标达成
                prog_l = abs(self.e_l.steps) / at_l if at_l else 1.0
                prog_r = abs(self.e_r.steps) / at_r if at_r else 1.0
                if min(prog_l, prog_r) >= 0.99:
                    break
                # ③ 直线修正：两轮同向且目标相近时，右轮追左轮里程
                straight = (tl * tr > 0) and abs(at_l - at_r) <= max(at_l, at_r) * 0.02
                if straight:
                    pr = speed + STRAIGHT_K * (abs(self.e_l.steps) / self.l_spm
                                                - abs(self.e_r.steps) / self.r_spm)
                    pr = max(0.12, min(0.9, pr))
                    pl = speed
                else:                              # 弧线/原地转：按目标比例给速
                    ratio = (at_r / at_l) if at_l and at_r else 1.0
                    pr = min(0.7, speed * max(1.0, ratio))
                    pl = min(0.7, speed * max(1.0, 1.0 / ratio if ratio else 1.0))
                self._drive((1 if tl >= 0 else -1) * pl, (1 if tr >= 0 else -1) * pr)
                # ④ 堵转：PWM 有输出但编码器 500ms 不动
                now = time.monotonic()
                cur = (self.e_l.steps, self.e_r.steps)
                if cur == last and now - last_t > STALL_MS / 1000:
                    self._stop()
                    speak_seq(["卡住了"])
                    print("   ⚠ 堵转，已断电")
                    return "stuck"
                if cur != last:
                    last, last_t = cur, now
                time.sleep(TICK)
        finally:
            self._stop()
            self.moving.clear()
        lm, rm = abs(self.e_l.steps) / self.l_spm, abs(self.e_r.steps) / self.r_spm
        print(f"   ✔ 实走：左 {lm:.2f} m / 右 {rm:.2f} m（目标 {abs(tl_m):.2f}）")
        return "done"

    def forward(self, dist, q):
        return self._run(dist, dist, q)

    def reverse(self, dist, q):
        return self._run(-dist, -dist, q)

    def turn(self, deg, q):
        """原地转；deg>0=右转。无 IMU，靠里程计，误差会累积（v0 接受）。
        实车方向反了 → 顶部 TURN_SIGN 改 -1。"""
        deg *= TURN_SIGN
        p = self.wheel_base / 2 * math.radians(abs(deg))       # 每轮弧长
        if deg > 0:      # 右转：左轮前进、右轮后退
            return self._run(p, -p, q, speed=min(SPEED, 0.3))
        return self._run(-p, p, q, speed=min(SPEED, 0.3))

    def arc(self, deg, dist, q):
        """定曲率弧线：走 dist 米、航向变化 deg 度（左前方=−45°）。
        内轮走 (R−W/2)θ，外轮走 (R+W/2)θ。"""
        deg *= TURN_SIGN
        th = math.radians(abs(deg))
        if th <= 0:
            return "noop"
        radius = dist / th
        half = self.wheel_base / 2
        inner = (radius - half) * th
        outer = (radius + half) * th
        if deg < 0:                      # 左前方：左轮在内
            return self._run(inner, outer, q)
        return self._run(outer, inner, q)  # 右前方：右轮在内


# ---------------------------------------------------------------- 子命令

TTS_VOICE = MODEL_DIR.parent / "tts" / "zh_CN-huayan-medium.onnx"   # piper 中文语音


def piper_synth(voice, text: str, out: Path):
    """piper 合成一句话 → wav（树莓派本地 TTS，07 架构的离线方案）。"""
    import wave
    chunks = list(voice.synthesize(text))
    if not chunks:
        raise RuntimeError(f"piper 没产出音频：{text}")
    audio = b"".join(c.audio_int16_bytes for c in chunks)
    sr = getattr(chunks[0], "sample_rate", 22050) or 22050
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio)


def cmd_prompts(argv=None):
    """生成播报音频：parts/ 拼接词段 + 整句提示。

    macOS：say 合成（⚠ 要在用户自己的终端跑——后台进程里 say -o 会写出空文件）。
    树莓派：piper 神经 TTS，直接生成到 ~/av_out/voice/，一步到位。
    """
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [(PARTS_DIR, t) for t in SPEAK_VERBS + SPEAK_CHARS + SPEAK_UNITS + SPEAK_MISC]
    jobs += [(PROMPT_DIR, t) for t in PROMPT_TEXT.values()]

    if sys.platform == "darwin":
        if not shutil.which("afconvert"):
            sys.exit("✗ 缺 afconvert")
        made = 0
        for out_dir, text in jobs:
            aiff, wav = out_dir / f"{text}.aiff", out_dir / f"{text}.wav"
            _, _, err = run(["say", "-v", "Tingting", text, "-o", str(aiff)])
            if not aiff.exists() or aiff.stat().st_size < 8000:
                print(f"✗ \"{text}\"：{err or 'say 输出为空（换到自己的终端跑）'}")
                continue
            run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)])
            aiff.unlink(missing_ok=True)
            made += 1
        print(f"✓ {made}/{len(jobs)} 条（say）")
        return

    try:
        from piper import PiperVoice
    except ImportError:
        sys.exit("✗ 缺 piper：~/leko-venv/bin/pip install piper-tts")
    if not TTS_VOICE.exists():
        sys.exit(f"✗ 缺中文语音 {TTS_VOICE}\n"
                 "  mkdir -p ~/models/tts && cd ~/models/tts\n"
                 "  curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx\n"
                 "  curl -LO https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json")
    voice = PiperVoice.load(str(TTS_VOICE))
    made = 0
    for out_dir, text in jobs:
        wav = out_dir / f"{text}.wav"
        try:
            piper_synth(voice, text, wav)
            made += 1
        except Exception as e:                          # noqa: BLE001
            print(f"✗ \"{text}\"：{e}")
    print(f"✓ {made}/{len(jobs)} 条（piper）→ parts/ + 整句")


def cmd_calib(argv=None):
    """推车 1 米标定编码器步/米 → voice_calib.json；`--default` 写内置推算值。"""
    args = [a for a in (argv or []) if a]

    if "--default" in args or "-d" in args:
        CALIB_FILE.write_text(json.dumps(DEFAULT_CALIB, indent=2))
        print(f"✓ 推算默认值 {SPM_EST} 步/米 → {CALIB_FILE.name}（±20%，下地后 calib 覆盖）")
        return

    # ---------- 实测流程：推车 1 米 ----------
    from gpiozero import RotaryEncoder
    e_l, e_r = RotaryEncoder(**ENC_L, max_steps=0), RotaryEncoder(**ENC_R, max_steps=0)
    ask("① 车放直线起点，回车开始计数…", "没下地用内置推算值，直接跑 drive 即可")
    e_l.steps = e_r.steps = 0
    ask("② 笔直推满 1.00 米，回车…")
    l, r = abs(e_l.steps), abs(e_r.steps)
    if l < 50 or r < 50:
        sys.exit(f"✗ 步数太少（左 {l} 右 {r}）：查编码器接线（先跑 02_encoder_test.py）")
    wb = WHEEL_BASE
    if sys.stdin.isatty():
        wb = float(input(f"③ 轮距（米，默认 {WHEEL_BASE}）：") or WHEEL_BASE)
    CALIB_FILE.write_text(json.dumps({"l_spm": l, "r_spm": r, "wheel_base": wb}, indent=2))
    print(f"✓ 左 {l} / 右 {r} 步/米，轮距 {wb} m → {CALIB_FILE.name}")


def cmd_drive(argv=None):
    d = Driver()
    ask("== 里程闭环自测：前进 1 米（留 2 米直线）== 回车开始…")
    print(f"结果：{d.forward(1.0, queue.Queue())}。走不准重跑 calib，跑偏调 STRAIGHT_K")


def read_wav_mono16k(path: Path):
    """读 WAV → (float32 numpy 数组[-1,1], 采样率)。非 16k 时线性重采样。"""
    import numpy as np
    import wave
    with wave.open(str(path), "rb") as w:
        rate, ch, width, n = (w.getframerate(), w.getnchannels(),
                              w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
    if width != 2:
        sys.exit(f"✗ 只支持 16-bit PCM：{path}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)          # 多声道 → 单声道
    a /= 32768.0
    if rate != 16000:                               # 简单线性重采样（测试样本够用）
        idx = np.arange(0, len(a), rate / 16000.0)
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a, 16000


def cmd_check(argv=None):
    """自检：音频过一遍 VAD + 识别 + 指令解析。

    默认用模型自带的 test_wavs；也可给路径指定自己的音频：
      python3 05_voice_control_test.py check ~/cmd/*.wav
    验证的是 Listener 完全相同的代码路径（只是音频来自文件而非 arecord），
    麦克风有毛病时也能确认依赖、模型、解析都装对了。
    """
    import numpy as np
    my_wavs = [Path(p) for p in (argv or [])]
    if my_wavs:
        wavs = [p for p in my_wavs if p.exists()]
        missing = [str(p) for p in my_wavs if not p.exists()]
        if missing:
            sys.exit(f"✗ 文件不存在：{missing}")
        src = "命令行指定"
    else:
        d = find_asr_dir()
        wavs = sorted((d / "test_wavs").glob("*.wav"))
        src = f"{d.name}/test_wavs"
    if not wavs:
        sys.exit("✗ 没有可用的样本音频")
    print(f"== 自检：{len(wavs)} 个音频（{src}）==")
    rec = load_asr()
    print("   ✓ 模型加载成功")
    vad, win = load_vad()
    print(f"   ✓ VAD 加载成功（窗口 {win} 样本 = {win/16000*1000:.0f}ms）")
    hit = total_seg = 0
    for w in wavs:
        a, _ = read_wav_mono16k(w)
        raw = (a * 32768.0).astype(np.int16).tobytes()
        segs = []
        for i in range(0, len(raw), win * 2):
            piece = raw[i:i + win * 2]
            if len(piece) < win * 2:                      # 补零到整窗，否则 VAD 报 Invalid n
                piece += b"\x00" * (win * 2 - len(piece))
            vad.accept_waveform(np.frombuffer(piece, dtype=np.int16))
            while not vad.empty():
                segs.append(vad.front.samples)
                vad.pop()
        vad.flush()
        while not vad.empty():
            segs.append(vad.front.samples)
            vad.pop()
        texts = [recognize(rec, s) for s in segs if len(s) >= 16000 * 0.25]
        texts = [t for t in texts if t]
        total_seg += len(texts)
        print(f"   🎧 {w.name} → {texts if texts else '（VAD 未切出语音段）'}")
        for t in texts:
            cmd = parse(t)
            print(f"        🗣 “{t}” → {cmd if cmd else '（无关，忽略）'}")
            hit += bool(cmd)
    print(f"== 自检完成：切出语音 {total_seg} 条，命中可执行指令 {hit} 条 ==")
    print("   下一步：calib → drive → mic → loop")


def cmd_parse(argv):
    """纯文本测解析器（不碰音频/模型）：python3 05_voice_control_test.py parse "前进一米" "向后半米"

    用来调词表最快——真实场景里 ASR 会把词听串，这里直接喂识别文本看会不会误判。
    """
    if not argv:
        sys.exit("✗ 用法：parse <一句话> [更多句…]")
    for t in argv:
        print(f"   🗣 “{t}” → {parse(t) or '（无关，忽略）'}")


def cmd_mic(argv=None):
    q = queue.Queue()
    print("== 听一句试试（说完停 0.6 秒断句；免唤醒直收，用于调麦克风）。Ctrl-C 退出 ==")
    Listener(q, threading.Event(), enable_kws=False).start()
    while True:
        time.sleep(0.5)


def cmd_wake(argv=None):
    if sys.platform == "darwin":
        sys.exit("✗ wake 在树莓派上跑（要麦克风）")
    ensure_volume()
    print("== 唤醒词自测：喊\"小豆/小豆小豆\"应答\"在\"；聊天/杂音不进识别。Ctrl-C 退出 ==")
    Listener(queue.Queue(), threading.Event()).start()
    while True:
        time.sleep(0.5)


def cmd_loop(argv=None):
    if sys.platform == "darwin":
        sys.exit("✗ loop 在树莓派上跑（要电机）")
    ensure_volume()
    moving = threading.Event()
    d = Driver(moving)
    q = queue.Queue()
    Listener(q, moving).start()
    print("== 语音控车主循环：喊\"小豆\"唤醒 → 说指令 ==")
    print("   指令：前进一米 / 后退半米 / 左前方前进1m / 左转九十度 / 掉头 / 停")
    print("   唤醒后 12 秒内免唤醒连说；行驶中\"停\"随时打断；执行完语音播报结果")
    try:
        while True:
            c = q.get()
            op = c["op"]
            rc = None
            if op == "stop":
                d._stop()
                speak_seq(["已停止"])
            elif op == "miss":
                speak_seq(["没找到指令"])
            elif op == "dist":
                if c.get("capped"):
                    print("   ⚠ 倒车限 1 米（前驱规则）")
                rc = d.reverse(c["dist"], q) if c["reverse"] else d.forward(c["dist"], q)
            elif op == "arc":
                rc = d.arc(c["deg"], c["dist"], q)
            elif op == "turn":
                rc = d.turn(c["deg"], q)
            if rc == "done":
                announce_cmd(c)                # 执行完播报（前进/一米/完毕）
    except KeyboardInterrupt:
        d._stop()
        print("\n已停止")


def main():
    subs = {"prompts": cmd_prompts, "calib": cmd_calib, "drive": cmd_drive,
            "check": cmd_check, "parse": cmd_parse, "mic": cmd_mic,
            "wake": cmd_wake, "loop": cmd_loop}
    takes_args = ("check", "parse", "calib")               # 只有这几个接受参数
    if len(sys.argv) < 2 or sys.argv[1] not in subs:
        print(__doc__)
        sys.exit(f"用法：python3 {Path(__file__).name} <{'/'.join(subs)}>")
    extra = sys.argv[2:]
    if extra and sys.argv[1] not in takes_args:
        sys.exit(f"✗ {sys.argv[1]} 不接受参数（多余：{' '.join(extra)}）")
    subs[sys.argv[1]](extra or None)


if __name__ == "__main__":
    main()
