#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""05 语音控车 v0 —— F1.1 精确指令，本地闭环（不经 ECS/dsh）

对应 07-软件架构设计 §五：精确指令走本地解析（断网可用），dsh 慢通道留给抽象任务。
语音急停例外：行驶中后台持续监听，"停"字随时打断。

用法（按顺序递进）
------------------
  python3 05_voice_control_test.py prompts   # ① 生成应答语音（在 macOS 跑一次，产物自动同步）
  python3 05_voice_control_test.py calib     # ② 校准编码器步/米（推车 1 米 + 尺量轮距，一次）
  python3 05_voice_control_test.py drive     # ③ 无语音自测：里程闭环走 1 米（先跑这个！）
  python3 05_voice_control_test.py mic       # ④ 听一句 → 打印识别与解析（不动车）
  python3 05_voice_control_test.py loop      # ⑤ 主循环：听 → 解析 → 执行

依赖（唯一的 pip）
------------------
  pip3 install sherpa-onnx          # 采集/播放用 arecord/aplay，别的不装

模型（一次性，约 70MB）
----------------------
  mkdir -p code/models/asr && cd code/models/asr
  curl -OL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2
  tar xf sherpa-onnx-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2
  mv sherpa-onnx-zipformer-small-bilingual-zh-en-2023-02-16 zipformer-zh-en
  curl -OL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
  # 若 404：到 github.com/k2-fsa/sherpa-onnx 的 releases 页 asr-models 标签下搜同名文件

播放音量坑（04_volume_ctl_test.py 已发现）
------------------------------------------
  绿联声卡 PCM 默认 0%！loop 启动会尝试自动设 90%，失败就先跑：
  python3 04_volume_ctl_test.py 100 -u

v0 安全边界（没有雷达/悬崖/急停硬件时的跑法）
---------------------------------------------
  ① 速度 PWM 0.35（约 0.25m/s）；单条指令 ≤ 3m；倒车 ≤ 1m（前驱规则）
  ② 堵转 500ms 自动断电；语音"停"随时打断；板上 ON/OFF 开关 = 人工急停
  ③ 只在空旷平地跑，人跟在旁边
"""

from __future__ import annotations

import json
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models" / "asr"
ASR_DIR = MODEL_DIR / "zipformer-zh-en"
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

PROMPT_TEXT = {
    "ok": "好嘞", "done": "到啦", "stop": "停",
    "miss": "没听清，请带上距离，比如前进一米", "stuck": "卡住了，检查一下",
}


# ---------------------------------------------------------------- 小工具

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        return 1, "", str(e)


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
        run(["aplay", "-q", str(wav)])


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
        rc, _, _ = run(["amixer", "-c", str(card), "set", "PCM", "90%", "unmute"])
        if rc == 0:
            print(f"   🔊 声卡 {card} PCM → 90%（若仍无声：python3 04_volume_ctl_test.py 100 -u）")


def capture_dev():
    _, out, _ = run(["arecord", "-l"])
    m = re.search(r"card (\d+)[^\n]*(?:ugreen|绿联|camera)", out, re.I)
    return f"plughw:{m.group(1)},0" if m else "default"


def load_calib():
    if not CALIB_FILE.exists():
        sys.exit("✗ 先跑 calib：python3 05_voice_control_test.py calib")
    d = json.loads(CALIB_FILE.read_text())
    for k in ("l_spm", "r_spm"):
        if not d.get(k):
            sys.exit(f"✗ calib 数据不完整（{k}），重跑 calib")
    return d


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


def _dist_in(text: str):
    m = re.search(r"([\d.]+|[零一二三四五六七八九十百两半]+(?:点[零一二三四五\d]+)?)"
                  r"(米|公尺|m|厘米|公分|cm|分米|dm)", text)
    if not m:
        return None
    v = cn2num(m.group(1))
    if v is None:
        return None
    unit = m.group(2)
    if unit in ("厘米", "公分", "cm"):
        v /= 100.0
    elif unit in ("分米", "dm"):
        v /= 10.0
    return round(min(v, MAX_DIST), 2)


def parse(text: str):
    """识别文本 → 指令 dict；None = 无关话。距离/角度缺省给 miss。"""
    if not text:
        return None
    t = re.sub(r"[\s，。,!？！？、'\"']", "", text.lower()).replace("１", "1")
    if re.search(r"停|刹车|别动|stop", t):
        return {"op": "stop"}
    dist = _dist_in(t)
    ang = None
    a = re.search(r"([零一二三四五六七八九十百两\d]+)度", t)
    if a:
        ang = cn2num(a.group(1))
    # 斜向（左前方等）→ 定曲率弧线
    for w, deg in (("左后方", -135), ("右后方", 135), ("左前方", -45), ("右前方", 45)):
        if w in t:
            return {"op": "arc", "deg": deg, "dist": dist} if dist else {"op": "miss"}
    if re.search(r"掉头|转身|转个圈", t):
        return {"op": "turn", "deg": 360 if "圈" in t else 180}
    if "左转" in t or "右转" in t:
        deg = abs(ang) if ang else 90
        return {"op": "turn", "deg": deg if "右转" in t else -deg}
    if re.search(r"前进|往前|向前|直行|直走", t):
        return {"op": "dist", "dist": dist, "reverse": False} if dist else {"op": "miss"}
    if re.search(r"后退|倒退|倒车|往后", t):
        if dist:
            return {"op": "dist", "dist": min(dist, MAX_REVERSE), "reverse": True,
                    "capped": dist > MAX_REVERSE}
        return {"op": "miss"}
    return None


# ---------------------------------------------------------------- ASR + VAD（sherpa-onnx）

def load_asr():
    if not ASR_DIR.exists():
        sys.exit(f"✗ 模型未下载：{ASR_DIR}\n  下载命令见本文件头部注释")
    import sherpa_onnx
    tok = sorted(ASR_DIR.glob("tokens.txt"))
    enc = sorted(ASR_DIR.glob("encoder*.onnx"))
    dec = sorted(ASR_DIR.glob("decoder*.onnx"))
    joi = sorted(ASR_DIR.glob("joiner*.onnx"))
    par = sorted(ASR_DIR.glob("model*.onnx"))
    if enc and dec and joi and tok:
        try:
            return sherpa_onnx.OfflineRecognizer.from_zipformer2(
                encoder=str(enc[0]), decoder=str(dec[0]), joiner=str(joi[0]),
                tokens=str(tok[0]), num_threads=2)
        except AttributeError:
            print("⚠ sherpa-onnx 版本偏旧：pip3 install -U sherpa-onnx")
            raise
    if par and tok:
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            model=str(par[0]), tokens=str(tok[0]), num_threads=2)
    sys.exit(f"✗ {ASR_DIR} 里找不到模型文件（encoder/decoder/joiner 或 model.onnx + tokens.txt）")


def load_vad():
    if not VAD_MODEL.exists():
        sys.exit(f"✗ VAD 模型未下载：{VAD_MODEL}")
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.6   # 静音 0.6s 断句
    cfg.silero_vad.min_speech_duration = 0.25
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30), \
        cfg.silero_vad.window_size


def recognize(rec, samples) -> str:
    stream = rec.create_stream()
    stream.accept_waveform(16000, samples)
    rec.decode(stream)
    return stream.result.text.strip()


class Listener(threading.Thread):
    """后台：麦克风 → VAD 断句 → 识别 → 解析 → 指令队列。异常只打日志不退出。"""

    def __init__(self, cmd_q: queue.Queue):
        super().__init__(daemon=True)
        self.q = cmd_q
        self.rec = load_asr()
        self.vad, self.window = load_vad()

    def _mic(self):
        dev = capture_dev()
        print(f"   🎤 采集设备：{dev}")
        return subprocess.Popen(
            ["arecord", "-D", dev, "-q", "-t", "raw", "-f", "S16_LE",
             "-r", "16000", "-c", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=-1)

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
                self.vad.accept_waveform(np.frombuffer(data, dtype=np.int16))
                while not self.vad.empty():
                    seg = self.vad.front.samples
                    self.vad.pop()
                    if len(seg) < 16000 * 0.25:
                        continue
                    text = recognize(self.rec, seg)
                    if not text:
                        continue
                    cmd = parse(text)
                    print(f"   🗣 “{text}” → {cmd if cmd else '（无关，忽略）'}")
                    if cmd:
                        self.q.put(cmd)
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
    def __init__(self):
        from gpiozero import Motor, RotaryEncoder
        self.m_l = Motor(**PIN_L)
        self.m_r = Motor(**PIN_R)
        self.e_l = RotaryEncoder(**ENC_L, max_steps=0)
        self.e_r = RotaryEncoder(**ENC_R, max_steps=0)
        c = load_calib()
        self.l_spm, self.r_spm = c["l_spm"], c["r_spm"]
        self.wheel_base = c.get("wheel_base", WHEEL_BASE)
        print(f"   🛞 校准：{self.l_spm:.0f}/{self.r_spm:.0f} 步/米，轮距 {self.wheel_base} m")

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
                        play("stop")
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
                    play("stuck")
                    print("   ⚠ 堵转，已断电")
                    return "stuck"
                if cur != last:
                    last, last_t = cur, now
                time.sleep(TICK)
        finally:
            self._stop()
        lm, rm = abs(self.e_l.steps) / self.l_spm, abs(self.e_r.steps) / self.r_spm
        print(f"   ✔ 实走：左 {lm:.2f} m / 右 {rm:.2f} m（目标 {abs(tl_m):.2f}）")
        play("done")
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

def cmd_prompts():
    if sys.platform != "darwin":
        sys.exit("✗ prompts 在 macOS 上跑（用系统 say 合成），产物在 code/av_out/voice/，连同仓库同步到树莓派")
    if not shutil.which("afconvert"):
        sys.exit("✗ 缺 afconvert")
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    voice = "Tingting"
    for name, text in PROMPT_TEXT.items():
        aiff = PROMPT_DIR / f"{name}.aiff"
        wav = PROMPT_DIR / f"{name}.wav"
        _, _, err = run(["say", "-v", voice, text, "-o", str(aiff)])
        if not aiff.exists():
            print(f"✗ {name}：{err}")
            continue
        run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(aiff), str(wav)])
        aiff.unlink(missing_ok=True)
        print(f"✓ {wav.name}  “{text}”")
    print("共 5 条应答音频。拷到树莓派：scp -r code pi:/home/xjs/")


def cmd_calib():
    from gpiozero import RotaryEncoder
    e_l = RotaryEncoder(**ENC_L, max_steps=0)
    e_r = RotaryEncoder(**ENC_R, max_steps=0)
    input("① 把车放在地面直线起点（贴 1 米胶带更好）。就绪后回车开始计数…")
    e_l.steps = e_r.steps = 0
    input("② 沿直线把车笔直推满 1.00 米，然后回车…")
    l, r = abs(e_l.steps), abs(e_r.steps)
    if l < 50 or r < 50:
        sys.exit(f"✗ 步数太少（左 {l} 右 {r}），检查编码器后重跑")
    wb = input(f"③ 尺量两驱动轮中心距（米，默认 {WHEEL_BASE}）：").strip() or str(WHEEL_BASE)
    data = {"l_spm": l, "r_spm": r, "wheel_base": float(wb)}
    CALIB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✓ 校准写入 {CALIB_FILE.name}：左 {l} 步/米，右 {r} 步/米，轮距 {wb} m")
    print("  （换轮/打滑多时重跑一次即可）")


def cmd_drive():
    d = Driver()
    q = queue.Queue()
    print("== 里程闭环自测：前进 1 米（地面留 2 米直线）==")
    input("回车开始…")
    rc = d.forward(1.0, q)
    print(f"结果：{rc}。误差 >5% 的话重跑 calib；向一侧跑偏 → 调大 STRAIGHT_K")


def cmd_mic():
    q = queue.Queue()
    print("== 听一句试试（说完停 0.6 秒断句）。Ctrl-C 退出，不会动车 ==")
    Listener(q).start()
    while True:
        time.sleep(0.5)


def cmd_loop():
    if sys.platform == "darwin":
        sys.exit("✗ loop 在树莓派上跑（要电机）")
    ensure_volume()
    d = Driver()
    q = queue.Queue()
    Listener(q).start()
    print("== 语音控车主循环。可以说：前进一米 / 后退半米 / 左前方前进一米 /")
    print("   左转九十度 / 右转 / 掉头 / 停。Ctrl-C 退出 ==")
    try:
        while True:
            c = q.get()
            op = c["op"]
            if op == "stop":
                d._stop()
                play("stop")
            elif op == "miss":
                play("miss")
            elif op == "dist":
                if c.get("capped"):
                    print(f"   ⚠ 倒车限 1 米（前驱规则）")
                play("ok")
                if c["reverse"]:
                    d.reverse(c["dist"], q)
                else:
                    d.forward(c["dist"], q)
            elif op == "arc":
                play("ok")
                d.arc(c["deg"], c["dist"], q)
            elif op == "turn":
                play("ok")
                d.turn(c["deg"], q)
    except KeyboardInterrupt:
        d._stop()
        print("\n已停止")


def main():
    subs = {"prompts": cmd_prompts, "calib": cmd_calib, "drive": cmd_drive,
            "mic": cmd_mic, "loop": cmd_loop}
    if len(sys.argv) != 2 or sys.argv[1] not in subs:
        print(__doc__)
        sys.exit(f"用法：python3 {Path(__file__).name} <{'/'.join(subs)}>")
    subs[sys.argv[1]]()


if __name__ == "__main__":
    main()
