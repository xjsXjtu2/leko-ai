"""voice：唤醒词（sherpa-onnx KeywordSpotter，「乐可」，纯本地）。

关键词文件按 tokens.txt 实测自动生成（拼音声母+带调韵母），拆不开会报错退出。
⚠ KWS 的 accept_waveform 只认 float32 归一化刻度——int16 会被当原始字节
读成垃圾、永不触发；VAD 没这问题，别弄混（kws_feed 已自动转换）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from leko.core.config import CONFIG, path

WAKE_WORDS = [(w, p) for w, p in CONFIG["wake"]["words"]]
KWS_THRESHOLD = CONFIG["wake"]["threshold"]      # 越大越难触发
KWS_DIR = path("kws_dir")
KWS_KEYWORDS_FILE = KWS_DIR.parent / "keywords.txt"

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
        sys.exit(f"✗ 唤醒词模型未下载：{KWS_DIR}\n  下载命令见 device_test/05 或 main.py 头部注释")
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
    """喂一小段音频给 KWS，命中返回唤醒词文本，否则 None。"""
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
        except Exception:                                   # noqa: BLE001
            pass
    return kw or None
