#!/usr/bin/env bash
# Jetson 端一键环境准备（由 speak.sh 自动调用，也可手动执行）
# 用法: bash setup_jet.sh [sudo密码]
set -euo pipefail

SUDO_PASS="${1:-1}"
BASE="$HOME/jetson-tts"
VOICE_DIR="$BASE/voices"
MODEL="zh_CN-huayan-medium"
# 国内用镜像源；无法访问镜像时自动回退官方源
HF_BASE="https://hf-mirror.com/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium"
HF_FALLBACK="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/zh/zh_CN/huayan/medium"

mkdir -p "$VOICE_DIR"

echo ">> [1/4] 创建 Python 虚拟环境"
if [ ! -x "$BASE/.venv/bin/python" ]; then
    python3 -m venv "$BASE/.venv"
fi
PY="$BASE/.venv/bin/python"

echo ">> [2/4] 安装 piper-tts（含 onnxruntime，可能需要几分钟）"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q piper-tts

echo ">> [3/4] 安装 espeak-ng-data（中文音素转换依赖）"
if ! dpkg -s espeak-ng-data >/dev/null 2>&1; then
    echo "$SUDO_PASS" | sudo -S apt-get install -y -q espeak-ng-data 2>/dev/null \
        || echo "!! espeak-ng-data 安装失败（如 piper 自带则可忽略）"
fi

echo ">> [4/4] 下载中文语音模型（约 63MB）"
for ext in onnx onnx.json; do
    dst="$VOICE_DIR/$MODEL.$ext"
    if [ -s "$dst" ]; then
        echo "   已存在: $dst"
        continue
    fi
    if ! curl -fsSL --retry 3 -o "$dst.tmp" "$HF_BASE/$MODEL.$ext"; then
        echo "   镜像源失败，改用官方源..."
        curl -fsSL --retry 3 -o "$dst.tmp" "$HF_FALLBACK/$MODEL.$ext"
    fi
    mv "$dst.tmp" "$dst"
done

echo ">> 验证 Piper"
"$PY" - "$VOICE_DIR/$MODEL.onnx" <<'EOF'
import sys
from piper import PiperVoice
v = PiperVoice.load(sys.argv[1])
print("Piper 就绪, sample_rate =", v.config.sample_rate)
EOF

echo ">> 环境准备完成"