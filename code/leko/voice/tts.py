"""voice：播报（TTS）—— Speaker 工人线程：一切播报走它，永不阻塞监听。

合成链：edge-tts（云端，微软晓晓，自然）→ 失败/断网 → piper（本地）兜底；
按文本缓存（av_out/cache）。播报期间置 PLAYING 事件 → 监听整块丢麦克风数据
（回声抑制：防喇叭播报被自己的麦克风识别、甚至自己触发前进）。"""
from __future__ import annotations

import hashlib
import queue
import sys
import threading
import time
from pathlib import Path

from leko.core.config import CONFIG, path
from leko.core.util import run
from leko.hal.audio import aplay

EDGE_VOICE = CONFIG["tts"]["edge_voice"]             # 中文（晓晓）：流程指令
EDGE_VOICE_EN = CONFIG["tts"].get("edge_voice_en", "en-US-AnaNeural")  # 英文（Ana 儿童声）：念单词
TTS_CACHE = path("tts_cache")
PROMPT_DIR = path("prompt_dir")
PARTS_DIR = PROMPT_DIR / "parts"       # 拼接词段（离线兜底用，prompts 子命令生成）
TTS_VOICE = path("tts_voice")           # piper 中文语音（本地兜底）
PLAYING = threading.Event()            # 播报中（含 0.3s 余量）：监听丢麦克风块

PROMPT_TEXT = {
    "ok": "好嘞", "done": "到啦", "stop": "停",
    "miss": "没听清，请带上距离，比如前进一米", "stuck": "卡住了，检查一下",
}
SPEAK_VERBS = ["前进", "后退", "左转", "右转", "掉头", "左前方前进", "右前方前进", "转个圈"]
SPEAK_CHARS = list("零一二三四五六七八九十点半百")
SPEAK_UNITS = ["米", "厘米", "公尺", "分米", "度"]
SPEAK_MISC = ["在", "完毕", "没找到指令", "已停止", "卡住了", "好嘞"]


def piper_synth(voice, text: str, out: Path):
    """piper 合成一句话 → wav（本地兜底，07 架构的离线方案）。"""
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


class Speaker(threading.Thread):
    """播放工人线程：合成+播放全在后台，监听线程永远实时。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self._piper = None
        self._lock = threading.Lock()

    def put(self, text, warm=False, voice=None):
        """voice=None → 中文；'en' → 英文儿童声（听写念单词）。"""
        v = EDGE_VOICE if voice in (None, "zh") else EDGE_VOICE_EN
        self.jobs.put((text, warm, v))

    def _synth_edge(self, text, v=EDGE_VOICE):
        import asyncio
        import edge_tts
        mp3 = TTS_CACHE / (hashlib.md5(f"{v}:{text}".encode()).hexdigest() + ".mp3")
        if not mp3.exists():
            async def go():
                await edge_tts.Communicate(text, v).save(str(mp3))
            asyncio.run(go())
        return mp3 if (mp3.exists() and mp3.stat().st_size > 2000) else None

    def _say_edge(self, text, v=EDGE_VOICE):
        try:
            mp3 = self._synth_edge(text, v)
        except Exception:            # noqa: BLE001  断网/缺包 → 让 piper 接手
            return False
        if mp3 is None:
            return False
        PLAYING.set()
        rc, _, _ = run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(mp3)])
        time.sleep(0.3)
        PLAYING.clear()
        return rc == 0

    def _say_piper(self, text):
        with self._lock:
            if self._piper is None:
                from piper import PiperVoice
                self._piper = PiperVoice.load(str(TTS_VOICE))
            wav = TTS_CACHE / (hashlib.md5(f"piper:{text}".encode()).hexdigest() + ".wav")
            if not wav.exists():
                piper_synth(self._piper, text, wav)
        PLAYING.set()
        aplay(wav)
        time.sleep(0.3)
        PLAYING.clear()

    def run(self):
        TTS_CACHE.mkdir(parents=True, exist_ok=True)
        while True:
            text, warm, v = self.jobs.get()
            try:
                if warm:                     # 只预热合成缓存，不出声
                    self._synth_edge(text, v)
                    continue
                if not self._say_edge(text, v):
                    self._say_piper(text)
            except Exception as e:           # noqa: BLE001
                print(f"   ⚠ 播报失败：{e}")
            finally:
                self.jobs.task_done()         # speak_wait 的 join 靠这个


SPEAKER = Speaker()
SPEAKER.start()


def speak(text: str, wait: bool = False):
    """播报中文一句话。wait=True：等这话说完再返回（听写流程用）。"""
    SPEAKER.put(text)
    if wait:
        SPEAKER.jobs.join()


def speak_wait(text: str):
    """播报中文一句话并等它说完（听写流程用：念完再计时/再等回应）。"""
    speak(text, wait=True)


def speak_en(text: str, wait: bool = False):
    """播报英文（Ana 儿童声）：念单词/拼读字母。"""
    SPEAKER.put(text, voice="en")
    if wait:
        SPEAKER.jobs.join()


def prewarm_tts():
    """预热常用播报的合成缓存（不出声）：首次应答不用等网络。"""
    for t in ("在", "已停止", "没找到指令", "卡住了"):
        SPEAKER.put(t, warm=True)


def prewarm_words(words: list):
    """预热听写词表全部音频（英文音色，不出声）：断网也照念（08 方案 §八）。"""
    for w in words:
        SPEAKER.put(w, warm=True, voice="en")


def speak_seq(parts, gap=0.06):
    """按序拼接播放词段 wav（离线零依赖兜底）；缺词段打字幕，不卡流程。"""
    miss = []
    for p in parts:
        wav = PARTS_DIR / f"{p}.wav"
        if wav.exists():
            if sys.platform == "darwin":
                run(["afplay", str(wav)])
            else:
                aplay(wav)
            time.sleep(gap)
        else:
            miss.append(p)
    if miss:
        print(f"   🔊（缺词段 {'/'.join(miss)}：跑 prompts 补）")
    print(f"   🔊 {' '.join(parts)}")
