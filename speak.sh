#!/usr/bin/env bash
# Mac 端：上传文本到 Jetson，Jetson 边推理边流式回传语音，本地实时播放
# 用法:
#   ./speak.sh 文章.txt                 # 发送文本（默认音色）
#   cat 文章.txt | ./speak.sh           # 或走 stdin
#   ./speak.sh --set-ref 参考音频.wav ["参考音频文字"]   # 设置克隆音色（之后默认使用）
#   ./speak.sh --clear-ref              # 恢复默认 Piper 音色
# 环境变量: JET_HOST(默认jet) JET_PASSWORD(默认1,仅首次安装密钥时使用)
set -euo pipefail

HOST="${JET_HOST:-jet}"
PASS="${JET_PASSWORD:-1}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY="$HOME/.ssh/id_ed25519_jettts"
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

# ---------- 连接工具（密钥免密；首次自动安装密钥，需 JET_PASSWORD） ----------
ensure_key() {
    if [ ! -f "$KEY" ]; then
        command -v sshpass >/dev/null 2>&1 || {
            echo "[错误] 首次使用需要 sshpass 来安装免密密钥: brew install sshpass" >&2
            exit 1
        }
        echo ">> 首次使用：生成并安装免密密钥到 $HOST ..."
        ssh-keygen -t ed25519 -N '' -C jet-tts -f "$KEY" -q
        for _ in 1 2 3 4 5; do
            sshpass -p "$PASS" ssh "${SSH_OPTS[@]:1}" \
                -o PubkeyAuthentication=no "$HOST" \
                'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' \
                < "$KEY.pub" && return 0
            sleep 1
        done
        echo "[错误] 密钥安装失败，请检查 JET_PASSWORD 与网络" >&2
        return 1
    fi
    ssh -n "${SSH_OPTS[@]}" "$HOST" 'exit' 2>/dev/null && return 0
    # 密钥失效（如账号重装过）：用密码重新安装
    command -v sshpass >/dev/null 2>&1 || return 1
    for _ in 1 2 3 4 5; do
        sshpass -p "$PASS" ssh "${SSH_OPTS[@]:1}" -o PubkeyAuthentication=no "$HOST" \
            'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' \
            < "$KEY.pub" && return 0
        sleep 1
    done
    return 1
}

run_ssh() {
    local tries=0
    until ssh -n "${SSH_OPTS[@]}" "$HOST" "$@"; do
        tries=$((tries+1))
        [ "$tries" -ge 4 ] && return 1
        sleep 1
    done
}

run_scp() {
    local tries=0
    until scp "${SSH_OPTS[@]}" "$@" </dev/null; do
        tries=$((tries+1))
        [ "$tries" -ge 4 ] && return 1
        sleep 1
    done
}

# ---------- Markdown 清洗（.md/.markdown 文件自动启用，仅保留正文） ----------
clean_markdown() {
    python3 -c '
import re, sys

text = open(sys.argv[1], encoding="utf-8").read()

# 代码块围栏
text = re.sub(r"```[^\n]*\n?", "", text)

# 图片/链接：保留显示文字，去掉地址
text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)

# 删除线/粗体/斜体/行内代码：只去符号、留内容
text = re.sub(r"~~([^~]+)~~", r"\1", text)
text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
text = re.sub(r"__([^_]+)__", r"\1", text)
text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
text = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", text)
text = re.sub(r"`([^`]*)`", r"\1", text)

out = []
for line in text.splitlines():
    line = line.strip()
    if not line:
        continue
    if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line):  # 水平分隔线：整行删除
        continue
    line = re.sub(r"^(#{1,6})\s+", "", line)        # 标题 #
    line = re.sub(r"^>\s?", "", line)                # 引用 >
    line = re.sub(r"^[-*+]\s+", "", line)            # 列表符号
    line = re.sub(r"<[^>]+>", "", line)              # HTML 标签
    line = line.strip()
    if line:
        out.append(line)

sys.stdout.write("\n".join(out) + "\n")
' "$1"
}

# ---------- 主流程 ----------
# 播放器可用性检查：sounddevice 播放器或 ffplay 二者有其一即可
if [ ! -x "$SRC_DIR/.venv/bin/python" ] || [ ! -f "$SRC_DIR/speak_player.py" ]; then
    if ! command -v ffplay >/dev/null 2>&1; then
        echo "[错误] 未找到 ffplay，请安装: brew install ffmpeg" >&2
        exit 1
    fi
fi

# ---------- 收尾：清理 md 临时文件 + 把流式 PCM 转成 WAV 保存 ----------
MD_TMP=""
OUT_PCM=""
OUT_WAV=""
SAMPLE_RATE=""
CH_OPT=()
cleanup() {
    [ -n "$MD_TMP" ] && rm -f "$MD_TMP"
    if [ -n "$OUT_PCM" ] && [ -s "$OUT_PCM" ]; then
        ffmpeg -loglevel error -f s16le -ar "${SAMPLE_RATE:-22050}" "${CH_OPT[@]}" \
            -i "$OUT_PCM" -y "$OUT_WAV" 2>/dev/null || true
        rm -f "$OUT_PCM"
    fi
}
trap cleanup EXIT

echo ">> 连接 $HOST ..."
ensure_key || { echo "[错误] 无法连接 $HOST" >&2; exit 1; }

# ---- 子命令：设置/清除 参考音频克隆 ----
case "${1:-}" in
    --set-ref)
        AUDIO="${2:-}"
        REF_TEXT="${3:-}"
        [ -n "$AUDIO" ] && [ -f "$AUDIO" ] || { echo "[错误] 参考音频不存在: ${AUDIO:-未指定}  用法: ./speak.sh --set-ref 参考音频.wav '参考音频对应文字'" >&2; exit 1; }
        [ -n "$REF_TEXT" ] || { echo "[错误] 需要提供参考音频对应的文字: ./speak.sh --set-ref 参考音频.wav '音频里说的文字'" >&2; exit 1; }
        echo ">> 上传参考音频《$(basename "$AUDIO")》并设为默认音色 ..."
        run_ssh "mkdir -p ~/jetson-tts/ref"
        run_scp "$AUDIO" "$HOST:~/jetson-tts/ref/reference.wav"
        printf '%s\n' "$REF_TEXT" | ssh "${SSH_OPTS[@]}" "$HOST" 'cat > ~/jetson-tts/ref/reference.txt'
        echo ">> 完成！之后所有语音默认使用该克隆音色（恢复 Piper: ./speak.sh --clear-ref）"
        exit 0
        ;;
    --clear-ref)
        run_ssh "rm -rf ~/jetson-tts/ref"
        echo ">> 已恢复默认 Piper 音色"
        exit 0
        ;;
esac

echo ">> 同步脚本到 Jetson ..."
run_ssh "mkdir -p ~/jetson-tts/voices"
run_scp "$SRC_DIR/piper_stream.py" "$SRC_DIR/clone_stream.py" "$HOST:~/jetson-tts/"

# 首次使用：自动初始化 Piper 环境
if ! run_ssh 'test -s ~/jetson-tts/voices/zh_CN-huayan-medium.onnx && test -x ~/jetson-tts/.venv/bin/python'; then
    echo ">> 首次使用，自动初始化 Jetson 环境（安装 piper-tts + 下载语音模型，约几分钟）..."
    run_scp "$SRC_DIR/setup_jet.sh" "$HOST:~/jetson-tts/"
    run_ssh "bash ~/jetson-tts/setup_jet.sh '$PASS'"
fi

# 引擎选择：已设置参考音频 → 调用 Jetson 上已部署的 GPT-SoVITS 克隆；否则 Piper
if run_ssh 'test -s ~/jetson-tts/ref/reference.wav'; then
    ENGINE=clone
    SAMPLE_RATE=32000   # GPT-SoVITS 输出固定 32kHz
    REMOTE_SCRIPT=clone_stream.py
    echo ">> 音色：参考音频克隆（GPT-SoVITS，端口 9880）"
    # 克隆服务自检：GPT-SoVITS 未在运行则自动启动（Jetson 重启后也无需手动操作）
    if ! run_ssh 'curl -s -o /dev/null --max-time 3 http://127.0.0.1:9880/'; then
        echo ">> GPT-SoVITS 服务未运行，自动启动中（加载模型约 1~2 分钟）..."
        run_ssh "cd ~/GPT-SoVITS && nohup ~/sovits-venv/bin/python api_v2.py -a None -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml >> api.log 2>&1 &"
        if ! run_ssh 'for i in $(seq 1 80); do curl -s -o /dev/null --max-time 2 http://127.0.0.1:9880/ && exit 0; sleep 3; done; exit 1'; then
            echo "[错误] GPT-SoVITS 服务启动失败，请登录 Jetson 检查: tail -f ~/GPT-SoVITS/api.log" >&2
            exit 1
        fi
        echo ">> GPT-SoVITS 服务已就绪"
    fi
else
    ENGINE=piper
    SAMPLE_RATE=22050
    REMOTE_SCRIPT=piper_stream.py
    echo ">> 音色：默认 Piper（--set-ref 可切换为参考音频克隆）"
fi

# 文本来源：命令行文件参数，或管道 stdin
if [ $# -ge 1 ] && [ "$1" != "-" ]; then
    INPUT_FILE="$1"
    [ -f "$INPUT_FILE" ] || { echo "[错误] 文件不存在: $INPUT_FILE" >&2; exit 1; }
    case "$INPUT_FILE" in
        *.md|*.markdown)
            MD_TMP="$(mktemp /tmp/speak-md.XXXXXX.txt)"
            clean_markdown "$INPUT_FILE" > "$MD_TMP"
            echo ">> 发送《$(basename "$INPUT_FILE")》（$(wc -m < "$INPUT_FILE" | tr -d ' ') 字，已清洗 Markdown 标记）"
            INPUT_REDIR="$MD_TMP"
            ;;
        *)
            echo ">> 发送《$(basename "$INPUT_FILE")》（$(wc -m < "$INPUT_FILE" | tr -d ' ') 字）"
            INPUT_REDIR="$INPUT_FILE"
            ;;
    esac
else
    echo ">> 从 stdin 读取文本"
    INPUT_REDIR=/dev/stdin
fi

echo ">> Jetson 流式合成中，开始播放（Ctrl+C 停止）..."
# ffmpeg 9+ 移除了 -ac 旧选项（改用 -ch_layout），低版本保持 -ac 1
FF_MAJOR="$(ffplay -version | sed -n 's/ffplay version \([0-9]*\).*/\1/p')"
if [ -n "$FF_MAJOR" ] && [ "$FF_MAJOR" -ge 9 ]; then
    CH_OPT=(-ch_layout mono)
else
    CH_OPT=(-ac 1)
fi
# 远端 stderr（进度）直接透传到终端，PCM 最简管道进 ffplay 实时播放（无 tee，避免缓冲干扰）
OUT_DIR="$(pwd)/voice"
mkdir -p "$OUT_DIR"
OUT_WAV="$OUT_DIR/$(date +%Y%m%d-%H%M%S)-$$.wav"
OUT_PCM="${OUT_WAV%.wav}.pcm"
echo ">> 录音将保存到: $OUT_WAV"
# 播放器：优先 sounddevice 回调播放器（按到达顺序播，不丢开头）；缺失时回退 ffplay
PLAYER_PY="$SRC_DIR/.venv/bin/python"
if [ -x "$PLAYER_PY" ] && [ -f "$SRC_DIR/speak_player.py" ]; then
    ssh "${SSH_OPTS[@]}" "$HOST" "exec ~/jetson-tts/.venv/bin/python ~/jetson-tts/$REMOTE_SCRIPT" \
        < "$INPUT_REDIR" \
    | "$PLAYER_PY" "$SRC_DIR/speak_player.py" "$SAMPLE_RATE"
else
    ssh "${SSH_OPTS[@]}" "$HOST" "exec ~/jetson-tts/.venv/bin/python ~/jetson-tts/$REMOTE_SCRIPT" \
        < "$INPUT_REDIR" \
    | ffplay -loglevel error -nodisp -autoexit -fflags nobuffer \
        -f s16le -ar "$SAMPLE_RATE" "${CH_OPT[@]}" -i pipe:0
fi

# 播放与保存解耦：Jetson 侧已同步落盘 out.pcm，播完拉回转 wav（远端用相对路径，base 是用户 home）
scp "${SSH_OPTS[@]}" "$HOST:jetson-tts/out.pcm" "$OUT_PCM" </dev/null 2>/dev/null || true
run_ssh "rm -f ~/jetson-tts/out.pcm" || true
SAVED_WAV="$OUT_WAV"
cleanup
OUT_PCM=""
OUT_WAV=""
echo ">> 播放结束，音频已保存: $SAVED_WAV"
[ -f "$SAVED_WAV" ] || echo ">> 提示: 本地未能生成 wav（远端临时文件拉取失败），但不影响本次播放"