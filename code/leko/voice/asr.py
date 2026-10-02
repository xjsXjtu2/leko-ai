"""voice：识别（SenseVoice/Paraformer 按目录自动选 + silero VAD 断句）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from leko.core.config import path

ASR_DIR = path("asr_dir")
VAD_MODEL = path("vad_model")


def find_asr_dir() -> Path:
    """定位模型目录：优先 ASR_DIR，否则在 ~/models/asr 下找唯一含 tokens.txt 的子目录。"""
    if ASR_DIR.exists():
        return ASR_DIR
    parent = ASR_DIR.parent
    cands = [d for d in parent.glob("*")
             if d.is_dir() and next(d.glob("tokens.txt"), None)]
    if len(cands) == 1:
        print(f"   ℹ 使用模型目录：{cands[0].name}")
        return cands[0]
    sys.exit(f"✗ 模型未下载或目录不唯一：{parent}\n  下载命令见 main.py 头部注释")


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
            tokens=str(tok[0]), num_threads=4,
            decoding_method="greedy_search")
    if par and "sense" in name:                   # SenseVoice（中英日韩粤）
        print("   ℹ 模型类型：sense_voice（int8，中英日韩粤）")
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(par[0]), tokens=str(tok[0]), num_threads=4,
            language="zh", use_itn=True)
    if par and "paraformer" in name:
        print("   ℹ 模型类型：paraformer")
        return sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(par[0]), tokens=str(tok[0]), num_threads=4)
    if par:                                       # zipformer-ctc 一类：单 model.onnx
        print("   ℹ 模型类型：zipformer_ctc（按单模型文件推断）")
        return sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
            model=str(par[0]), tokens=str(tok[0]), num_threads=4)
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


def read_wav_mono16k(wav: Path):
    """读 WAV → (float32 [-1,1], 16000)。非 16k 线性重采样（自检样本够用）。"""
    import numpy as np
    import wave
    with wave.open(str(wav), "rb") as w:
        rate, ch, width, n = (w.getframerate(), w.getnchannels(),
                              w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
    if width != 2:
        sys.exit(f"✗ 只支持 16-bit PCM：{wav}")
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)          # 多声道 → 单声道
    a /= 32768.0
    if rate != 16000:
        idx = np.arange(0, len(a), rate / 16000.0)
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a, 16000
