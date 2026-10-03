#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""leko 主入口 —— 语音控车（F1.1 精确指令，本地闭环，不经 ECS/dsh）

对应 07-软件架构设计 §五：精确指令走本地解析（断网可用），dsh 慢通道留给抽象任务。
语音急停例外：行驶中后台持续监听，"停"字随时打断。

用法（按顺序递进；与旧 05_voice_control_test.py 完全同构）
------------------
  python3 ~/leko/main.py check                 # 自检：无麦克风验证模型+VAD+解析
  python3 ~/leko/main.py check ~/cmd_test/*.wav # 用指定音频自检
  python3 ~/leko/main.py parse "前进一米" "向后半米"   # 纯文本调词表（不加载模型）
  python3 ~/leko/main.py prompts                # 生成离线兜底词段（piper/say）
  python3 ~/leko/main.py calib                 # 校准编码器步/米（推车 1 米）
  python3 ~/leko/main.py calib --default        # 只写推算值
  python3 ~/leko/main.py drive                 # 无语音自测：里程闭环走 1 米
  python3 ~/leko/main.py mic                   # 听一句 → 打印识别与解析（不动车）
  python3 ~/leko/main.py wake                  # 唤醒词自测（喊"乐可"应答，不动车）
  python3 ~/leko/main.py dictation            # 英语听写（F2.0；免唤醒调试模式）
  python3 ~/leko/main.py loop                  # 主循环：唤醒词 → 听 → 解析 → 执行 → 播报

依赖与模型（全部内聚在仓库目录，gitignore 不进 git）
------------------
  python3 -m venv --system-site-packages <仓库>/leko-venv      # apt 的 gpiozero/numpy 继续可见
  <仓库>/leko-venv/bin/pip install sherpa-onnx edge-tts piper-tts pyyaml
  bash code/download_models.sh                                 # 模型一键下载（约 280MB → <仓库>/models/）
  其余依赖全来自 apt：gpiozero / numpy / alsa-utils / ffmpeg（ffplay）

模型（一次性，约 250MB，路径锚定 ~，与 ~/leko 部署位置无关）
----------------------
  R=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
  mkdir -p ~/models/asr && cd ~/models/asr
  curl -OL $R/silero_vad.onnx
  curl -OL $R/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
  tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2 && rm *.tar.bz2
  # 唤醒词模型（注意 tag 是 kws-models，不是 asr-models）
  K=https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models
  curl -OL $K/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
  tar xf sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2 && rm sherpa-onnx-kws*.tar.bz2
  # piper 本地兜底语音（国内网络走 hf-mirror.com）
  mkdir -p ~/models/tts && cd ~/models/tts
  curl -OL https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
  curl -OL https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json

v0 安全边界（没有雷达/悬崖/急停硬件时的跑法）
---------------------------------------------
  ① 速度 PWM 0.35（约 0.25m/s）；单条指令 ≤ 3m；倒车 ≤ 1m（前驱规则）
  ② 堵转 500ms 自动断电；语音"停"随时打断；板上 ON/OFF 开关 = 人工急停
  ③ 只在空旷平地跑，人跟在旁边
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path

# ---------------- 运行环境自举：系统 python3 → ~/leko-venv ----------------
VENV_PY = Path(__file__).resolve().parents[2] / "leko-venv" / "bin" / "python3"


def _bootstrap():
    if sys.prefix != sys.base_prefix:            # 已经在 venv 里，直接继续
        return
    if not VENV_PY.exists():
        print(f"⚠ 没找到 {VENV_PY}，用系统 Python 继续（依赖靠 apt）")
        print("  装依赖：")
        print("    python3 -m venv --system-site-packages ~/leko-venv")
        print("    ~/leko-venv/bin/pip install sherpa-onnx edge-tts")
        return
    argv = [str(VENV_PY)] + ([] if sys.stdout.isatty() else ["-u"])
    argv += [str(Path(__file__).resolve()), *sys.argv[1:]]
    os.execv(str(VENV_PY), argv)


_bootstrap()

# ---------------- 包路径：~/leko/main.py 直接跑也要能 import leko.* ----------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from leko.core.config import CONFIG, load_calib, path, save_calib       # noqa: E402
from leko.core.util import ask, run                                      # noqa: E402
from leko.ctrl.intent import parse, phrase_words                        # noqa: E402
from leko.ctrl.pilot import Pilot                                        # noqa: E402
from leko.hal.audio import ensure_volume, seg_dbfs, start_mic           # noqa: E402
from leko.voice.asr import (find_asr_dir, load_asr, load_vad,       # noqa: E402
                             read_wav_mono16k, recognize, recognize_command)
from leko.voice.clips import keep_vad_clip                            # noqa: E402
from leko.voice.tts import (PARTS_DIR, PROMPT_DIR, PROMPT_TEXT,          # noqa: E402
                            SPEAK_CHARS, SPEAK_MISC, SPEAK_UNITS, SPEAK_VERBS,
                            piper_synth, prewarm_tts, speak)
from leko.voice.wake import kws_feed, load_kws                           # noqa: E402

CONTINUE_WINDOW = CONFIG["wake"]["continue_window_s"]
PREROLL_S = CONFIG["wake"]["preroll_s"]
JUNK_DBFS = CONFIG["safety"]["junk_dbfs"]


# ---------------- 播报整句（执行结果 → 一句话） ----------------

def _say_dist(cmd: dict) -> str:
    """命中原文 → 适合念的中文：'2.5m' → '二点五米'。"""
    return "".join(phrase_words(cmd.get("dtxt") or f"{cmd['dist']}米"))


def announce_cmd(cmd: dict):
    """执行成功后的语音通报：整句合成（edge-tts 自然语调）。"""
    op = cmd.get("op")
    if op == "dist":
        verb = "后退" if cmd["reverse"] else "前进"
        d = "一米" if cmd.get("capped") else _say_dist(cmd)
        speak(f"{verb}{d}，完毕")
    elif op == "arc":
        d = "左前方" if cmd["deg"] < 0 else "右前方"
        speak(f"{d}前进{_say_dist(cmd)}，完毕")
    elif op == "turn":
        deg = abs(cmd["deg"])
        if deg >= 360:
            speak("转个圈，完毕")
        elif deg >= 180:
            speak("掉头，完毕")
        else:
            w = "右转" if cmd["deg"] > 0 else "左转"
            ang = "".join(phrase_words(cmd.get("atxt") or f"{deg}度"))
            speak(f"{w}{ang}，完毕")


# ---------------- 监听状态机：WAKE（只跑唤醒词）↔ ARMED（VAD+ASR 收指令） ----------------

class Listener(threading.Thread):
    """后台：唤醒词（KWS）→ 指令会话（VAD+ASR）→ 指令队列。

    状态机（把杂音挡在 ASR 之外，也给了指令明确边界）：
      WAKE  只跑唤醒词（聊天/电视声不进 ASR）
             ↓ 喊"乐可/乐可乐可" → 应答"在"
      ARMED VAD+ASR 收指令（预缓冲拼回段首，补 VAD 削掉的首字）
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
        self.text_hook = None            # 技能会话钩子（听写）：短指令优先于全局解析
        self.preroll = deque(maxlen=max(1, int(16000 * PREROLL_S / self.window)))

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

    def _fresh_ears(self, proc):
        """清耳（唤醒时/回睡时）：重启麦克风扔掉管道积压 + 重建 VAD 丢半截段 + 清预缓冲。

        识别/播报偶尔卡顿会让旧音频在管道里排队——越排越多，就是"延时变大"。
        """
        try:
            proc.kill()
        except Exception:                       # noqa: BLE001
            pass
        proc = start_mic()
        self.vad, self.window = load_vad()
        self.preroll.clear()
        return proc

    def run(self):
        import numpy as np
        proc = start_mic()
        chunk = self.window * 2                       # int16 → 字节数
        from leko.voice.tts import PLAYING            # 回声抑制：播报中丢麦克风块
        while True:
            try:
                data = proc.stdout.read(chunk)
                if not data:
                    proc = start_mic()
                    continue
                samples = np.frombuffer(data, dtype=np.int16)
                if self.armed and PLAYING.is_set():     # 播报中：麦克风收的是自己的
                    continue                            # 回声，整块丢弃（防自识别）
                self.preroll.append(samples)
                if not self.armed:                    # ---- WAKE：只跑唤醒词 ----
                    kw = kws_feed(self.kws, self.stream, samples)
                    if kw:
                        print(f"   🐟 唤醒：\"{kw}\"")
                        speak("在")
                        proc = self._fresh_ears(proc)   # 清耳：扔掉积压旧音频 + 重建 VAD
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
                    text, src = recognize_command(self.rec, audio)
                    clip = keep_vad_clip(audio, text)
                    if clip is not None:
                        print(f"   📼 {clip.name}")
                    if not text:
                        continue
                    if self.text_hook is not None:      # 技能会话内：会话短指令优先
                        cmd = self.text_hook(text)
                    else:
                        cmd = parse(text)
                    if cmd is None and len(text) <= 2:  # "嗯/啊"短杂音：不刷屏
                        continue
                    print(f"   🗣 {src} \"{text}\" → {cmd if cmd else '（无关，忽略）'}")
                    if cmd:
                        self.q.put(cmd)
                        self.last_act = time.monotonic()
                if (not self.moving.is_set()
                        and time.monotonic() - self.last_act > CONTINUE_WINDOW):
                    self.armed = False
                    proc = self._fresh_ears(proc)
                    print("   💤 超时无指令，回到待唤醒（喊\"乐可\"）")
            except Exception as e:                     # noqa: BLE001
                print(f"   ⚠ 监听异常：{e}（重启麦克风）")
                time.sleep(1)
                try:
                    proc.kill()
                except Exception:                     # noqa: BLE001
                    pass
                proc = start_mic()


# ---------------- 子命令 ----------------

def cmd_prompts(argv=None):
    """生成离线兜底播报词段（edge-tts 云端优先后，词段只是断网保险）。"""
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
    from leko.voice.tts import TTS_VOICE
    if not TTS_VOICE.exists():
        sys.exit(f"✗ 缺中文语音 {TTS_VOICE}\n  下载命令见本文件头部注释（hf-mirror）")
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
    """推车 1 米标定编码器步/米 → voice_calib.json；`--default` 只写推算值。"""
    from leko.core.config import DEFAULT_CALIB, SPM_EST
    args = [a for a in (argv or []) if a]

    if "--default" in args or "-d" in args:
        f = save_calib(DEFAULT_CALIB["l_spm"], DEFAULT_CALIB["r_spm"],
                       DEFAULT_CALIB["wheel_base"])
        print(f"✓ 推算默认值 {SPM_EST} 步/米 → {f.name}（±20%，下地后 calib 覆盖）")
        return

    from gpiozero import RotaryEncoder
    from leko.core.config import CONFIG as cfg
    pins = cfg["pins"]
    e_l = RotaryEncoder(**pins["encoder_left"], max_steps=0)
    e_r = RotaryEncoder(**pins["encoder_right"], max_steps=0)
    ask("① 车放直线起点，回车开始计数…", "没下地用内置推算值，直接跑 drive 即可")
    e_l.steps = e_r.steps = 0
    ask("② 笔直推满 1.00 米，回车…")
    l, r = abs(e_l.steps), abs(e_r.steps)
    if l < 50 or r < 50:
        sys.exit(f"✗ 步数太少（左 {l} 右 {r}）：查编码器接线（先跑 device_test/02_encoder_test.py）")
    wb = CONFIG["motion"]["wheel_base_m"]
    if sys.stdin.isatty():
        wb = float(input(f"③ 轮距（米，默认 {wb}）：") or wb)
    f = save_calib(l, r, wb)
    print(f"✓ 左 {l} / 右 {r} 步/米，轮距 {wb} m → {f.name}")


def cmd_drive(argv=None):
    d = Pilot()
    ask("== 里程闭环自测：前进 1 米（留 2 米直线）== 回车开始…")
    print(f"结果：{d.forward(1.0, queue.Queue())}。走不准重跑 calib，跑偏调 config motion.straight_k")


def cmd_check(argv=None):
    """自检：音频过一遍 VAD + 识别 + 指令解析（与 Listener 完全相同的代码路径）。"""
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
        samples = (a * 32768.0).astype(np.int16)
        segs = []
        for i in range(0, len(samples), win):
            piece = samples[i:i + win]
            if len(piece) < win:                          # 补零到整窗，否则 VAD 报 Invalid n
                piece = np.concatenate([piece, np.zeros(win - len(piece), np.int16)])
            vad.accept_waveform(piece)
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
            print(f"        🗣 \"{t}\" → {cmd if cmd else '（无关，忽略）'}")
            hit += bool(cmd)
    print(f"== 自检完成：切出语音 {total_seg} 条，命中可执行指令 {hit} 条 ==")
    print("   下一步：calib → drive → mic → wake → loop")


def cmd_parse(argv):
    """纯文本测意图解析（不碰音频/模型）。"""
    if not argv:
        sys.exit("✗ 用法：parse <一句话> [更多句…]")
    for t in argv:
        print(f"   🗣 \"{t}\" → {parse(t) or '（无关，忽略）'}")


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
    prewarm_tts()
    print("== 唤醒词自测：喊\"乐可/乐可乐可\"应答\"在\"；聊天/杂音不进识别。Ctrl-C 退出 ==")
    Listener(queue.Queue(), threading.Event()).start()
    while True:
        time.sleep(0.5)


def cmd_dictation(argv=None):
    """英语听写独立入口（免唤醒直进，联调/验收用；正式入口在 loop 里喊"乐可，听写"）。"""
    if sys.platform == "darwin":
        sys.exit("✗ dictation 在树莓派上跑（要相机+麦克风）")
    ensure_volume()
    prewarm_tts()
    q = queue.Queue()
    listener = Listener(q, threading.Event(), enable_kws=False)   # 会话态：免唤醒直收
    listener.start()
    print("== 英语听写（免唤醒调试模式）。喊：好了 / 写完了 / 再来一遍 / 下一个 / 结束 ==")
    try:
        from leko.voice.dictation import DictationSession
        summary = DictationSession(q, listener).run()
        print("会话摘要：", summary)
    except KeyboardInterrupt:
        print("\n已退出")


def cmd_loop(argv=None):
    if sys.platform == "darwin":
        sys.exit("✗ loop 在树莓派上跑（要电机）")
    ensure_volume()
    moving = threading.Event()
    d = Pilot(moving)
    q = queue.Queue()
    listener = Listener(q, moving)
    listener.start()
    prewarm_tts()
    print("== 语音控车主循环：喊\"乐可\"唤醒 → 说指令 ==")
    print("   指令：前进一米 / 后退半米 / 左前方前进1m / 左转九十度 / 掉头 / 调头 / 停")
    print("   唤醒后 12 秒内免唤醒连说；行驶中\"停\"随时打断；执行完语音播报结果")
    try:
        while True:
            c = q.get()
            op = c["op"]
            rc = None
            if op == "stop":
                d._stop()
                speak("已停止")
            elif op == "miss":
                speak("没找到指令")
            elif op == "dist":
                if c.get("capped"):
                    print("   ⚠ 倒车限 1 米（前驱规则）")
                rc = d.reverse(c["dist"], q) if c["reverse"] else d.forward(c["dist"], q)
            elif op == "arc":
                rc = d.arc(c["deg"], c["dist"], q)
            elif op == "turn":
                rc = d.turn(c["deg"], q)
            elif op == "dictation":                       # F2.0 英语听写（08 方案）
                from leko.voice.dictation import DictationSession
                DictationSession(q, listener).run()
            if rc == "done":
                announce_cmd(c)                # 执行完播报（前进/一米/完毕）
    except KeyboardInterrupt:
        d._stop()
        print("\n已停止")


def main():
    subs = {"prompts": cmd_prompts, "calib": cmd_calib, "drive": cmd_drive,
            "check": cmd_check, "parse": cmd_parse, "mic": cmd_mic,
            "wake": cmd_wake, "dictation": cmd_dictation, "loop": cmd_loop}
    takes_args = ("check", "parse", "calib")
    if len(sys.argv) < 2 or sys.argv[1] not in subs:
        print(__doc__)
        sys.exit(f"用法：python3 {Path(__file__).name} <{'/'.join(subs)}>")
    extra = sys.argv[2:]
    if extra and sys.argv[1] not in takes_args:
        sys.exit(f"✗ {sys.argv[1]} 不接受参数（多余：{' '.join(extra)}）")
    subs[sys.argv[1]](extra or None)


if __name__ == "__main__":
    main()
