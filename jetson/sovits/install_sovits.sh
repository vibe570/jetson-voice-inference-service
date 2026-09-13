#!/usr/bin/env bash
# Jetson 端从 0 部署 GPT-SoVITS(基于官方仓库 + CUDA 推理配置)
# 适用: JetPack 6.x(Orin 系列),已联网;需要 sudo 密码用于 apt
# 用法: bash install_sovits.sh [sudo密码] [安装目录]
set -euo pipefail

SUDO_PASS="${1:-1}"
INSTALL_DIR="${2:-$HOME/GPT-SoVITS}"
REPO_URL="https://github.com/RVC-Boss/GPT-SoVITS.git"
PIN_COMMIT="48b1a01"          # 在 Orin Nano Super / JetPack 6.2 上验证过的官方提交
PY_TORCH_INDEX="https://download.pytorch.org/whl/cu132"
HF_ENDPOINT="https://hf-mirror.com"   # 国内镜像;海外可去掉该行直接用官方
HF_REPO="lj1995/GPT-SoVITS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> [1/6] 安装系统依赖 (espeak-ng-data 等)"
echo "$SUDO_PASS" | sudo -S apt-get install -y -q espeak-ng-data libsndfile1 ffmpeg 2>/dev/null || true

echo "==> [2/6] 拉取官方 GPT-SoVITS 源码 (commit $PIN_COMMIT)"
if [ ! -d "$INSTALL_DIR/.git" ]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"
git fetch --quiet origin "$PIN_COMMIT" && git checkout --quiet "$PIN_COMMIT"

echo "==> [3/6] 创建 Python 虚拟环境并安装 PyTorch (CUDA, aarch64)"
if [ ! -x "$INSTALL_DIR/sovits-venv/bin/python" ]; then
    python3 -m venv "$INSTALL_DIR/sovits-venv"
fi
PY="$INSTALL_DIR/sovits-venv/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q torch==2.12.1+cu132 torchaudio==2.11.0 --index-url "$PY_TORCH_INDEX"

echo "==> [4/6] 安装官方依赖 (requirements.txt)"
"$PY" -m pip install -q -r requirements.txt

echo "==> [5/6] 下载官方预训练模型 (hf-mirror)"
"$PY" -m pip install -q huggingface_hub
HF_ENDPOINT="$HF_ENDPOINT" "$PY" -m huggingface_hub.commands.huggingface_cli download "$HF_REPO" \
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large" \
    "GPT_SoVITS/pretrained_models/chinese-hubert-base" \
    "GPT_SoVITS/pretrained_models/s1v3.ckpt" \
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth" \
    --local-dir "$INSTALL_DIR" --resume-download

echo "==> [6/6] 放置本仓库的部署文件 (api_v2.py + 默认配置)"
cp "$SCRIPT_DIR/api_v2.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/tts_infer.vanilla.yaml" "$INSTALL_DIR/GPT_SoVITS/configs/tts_infer.yaml"

echo ""
echo "==> 部署完成！启动服务:"
echo "   cd $INSTALL_DIR && nohup ./sovits-venv/bin/python api_v2.py -a None -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml >> api.log 2>&1 &"
echo "   验证: curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9880/"
echo ""
echo "提示: 如需使用自训练权重(如 zizi1 微调),把权重放到对应目录后改用 tts_infer.yaml"