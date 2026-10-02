#!/usr/bin/env bash
# 一键下载全部模型（约 280MB → 仓库内 models/，gitignore 不进 git，与代码内聚）。
# 幂等：已存在的跳过。国内网络：piper 语音走 hf-mirror.com。
# 用法：bash code/download_models.sh（在仓库任意位置克隆都能跑）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"      # 仓库根
cd "$ROOT"
R=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
K=https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models
M=https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium

mkdir -p "$ROOT/models/asr" "$ROOT/models/tts"

# --- ASR：SenseVoice（中英日韩粤，int8）+ silero VAD ---
cd "$ROOT/models/asr"
[ -f silero_vad.onnx ] || curl -L -O "$R/silero_vad.onnx"
D=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
[ -d "$D" ] || { curl -L -O "$R/$D.tar.bz2" && tar xf "$D.tar.bz2" && rm "$D.tar.bz2"; }

# --- 唤醒词：KWS zipformer（tag 是 kws-models，不是 asr-models！） ---
KD=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
[ -d "$KD" ] || { curl -L -O "$K/$KD.tar.bz2" && tar xf "$KD.tar.bz2" && rm "$KD.tar.bz2"; }

# --- TTS 兜底：piper 中文语音（直连 HF 国内不通，走 hf-mirror） ---
cd "$ROOT/models/tts"
[ -f zh_CN-huayan-medium.onnx ] || curl -L -O "$M/zh_CN-huayan-medium.onnx"
[ -f zh_CN-huayan-medium.onnx.json ] || curl -L -O "$M/zh_CN-huayan-medium.onnx.json"

echo "✓ 模型就绪："
du -sh "$ROOT"/models/asr/* "$ROOT"/models/tts/* 2>/dev/null
echo "  venv 依赖（一次性）：$ROOT/leko-venv/bin/pip install sherpa-onnx edge-tts piper-tts pyyaml"
