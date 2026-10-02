"""配置装载：leko/config.yaml（唯一事实源）+ 编码器校准 voice_calib.json。

yaml 缺失/损坏/pyyaml 未装时退回内置默认值（与 yaml 同构），功能不受阻。"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent          # .../code/leko
REPO_ROOT = PKG_DIR.parent.parent                        # 仓库根（如 ~/leko-ai）

DEFAULTS: dict = {
    "paths": {
        "asr_dir": "models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "kws_dir": "models/asr/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
        "vad_model": "models/asr/silero_vad.onnx",
        "tts_voice": "models/tts/zh_CN-huayan-medium.onnx",
        "prompt_dir": "av_out/voice",
        "tts_cache": "av_out/cache",
        "calib_file": "voice_calib.json",
    },
    "pins": {
        "motor_left": {"in1": 23, "in2": 24, "pwm": 12, "invert": False},
        "motor_right": {"in1": 17, "in2": 27, "pwm": 13, "invert": False},
        "encoder_left": {"a": 5, "b": 6},
        "encoder_right": {"a": 25, "b": 16},
    },
    "wake": {
        "words": [["小树莓", "xiao3 shu4 mei2"],
                  ["小树莓小树莓", "xiao3 shu4 mei2 xiao3 shu4 mei2"]],
        "threshold": 0.25, "continue_window_s": 12, "preroll_s": 0.30,
    },
    "motion": {"speed_pwm": 0.35, "max_dist_m": 3.0, "max_reverse_m": 1.0,
               "wheel_base_m": 0.17, "straight_k": 4.0, "turn_sign": 1, "tick_s": 0.02},
    "safety": {"stall_ms": 500, "junk_dbfs": -45.0},
    "tts": {"edge_voice": "zh-CN-XiaoxiaoNeural"},
}


def _resolve(p: str) -> Path:
    """相对路径 → 仓库根；~/开头 → 家目录；绝对路径原样。"""
    q = Path(p)
    if p.startswith("~") or q.is_absolute():
        return q.expanduser()
    return REPO_ROOT / q


def _load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        import yaml
        with open(PKG_DIR / "config.yaml", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception as e:                                  # noqa: BLE001
        print(f"⚠ config.yaml 载入失败（{e}），用内置默认值")
    return cfg


CONFIG = _load()


def path(key: str) -> Path:
    """paths.<key> → 绝对 Path（相对=仓库根）。"""
    return _resolve(str(CONFIG["paths"][key]))


# ---------------- 编码器校准（voice_calib.json） ----------------

# 520 电机 11PPR × 减速比 30 × 四倍频 = 1320 步/圈 ÷ 65mm 轮周长 ≈ 6464，误差 ±20%
SPM_EST = round(11 * 30 * 4 / (math.pi * 0.065))
DEFAULT_CALIB = {"l_spm": SPM_EST, "r_spm": SPM_EST,
                 "wheel_base": CONFIG["motion"]["wheel_base_m"]}


def load_calib() -> dict:
    """有校准文件就用，否则用推算默认值（不影响安全，只是走得准不准）。"""
    try:
        d = json.loads(path("calib_file").read_text())
        if d.get("l_spm") and d.get("r_spm"):
            return {**DEFAULT_CALIB, **d}
    except Exception:                                       # noqa: BLE001
        pass
    print(f"   ⚠ 用推算默认值 {SPM_EST} 步/米（下地后跑 calib 校准）")
    return dict(DEFAULT_CALIB)


def save_calib(l_spm: int, r_spm: int, wheel_base: float) -> Path:
    f = path("calib_file")
    f.write_text(json.dumps({"l_spm": l_spm, "r_spm": r_spm,
                             "wheel_base": wheel_base}, indent=2))
    return f
