#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson 端流式语音合成脚本（配合 Mac 端 speak.sh 使用）
- 从 stdin 读取长文本（支持万字以上）
- 按句切分，逐段流式合成（边推理边输出，不等全文）
- 原始 PCM (s16le 单声道) 写入 stdout，进度/错误信息写入 stderr
"""
import os
import re
import sys
import time

MODEL_PATH = os.path.expanduser("~/jetson-tts/voices/zh_CN-huayan-medium.onnx")
SAMPLE_RATE_FALLBACK = 22050  # huayan 模型的采样率，声卡/播放端需要一致

SENTENCE_END = "。！？!?…；;"
SOFT_DELIM = "，,、：:"
MAX_CHUNK = 100   # 每段最大字符数（约 20~25 秒音频），控制首段延迟
MERGE_UPTO = 120  # 相邻小段合并上限


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _hard_split(p: str, max_chunk: int):
    """超长句按逗号等软分隔符切分（找不到分隔符则硬切）。"""
    out = []
    while len(p) > max_chunk:
        head = p[:max_chunk]
        m = list(re.finditer(f"[{SOFT_DELIM} ]", head))
        cut = m[-1].end() if m else max_chunk
        seg = p[:cut].strip()
        if seg:
            out.append(seg)
        p = p[cut:].lstrip()
    if p.strip():
        out.append(p.strip())
    return out


def split_text(text: str, max_chunk: int = MAX_CHUNK):
    """长文切块：优先按句末标点切句，短句合并，超长句再软切。"""
    parts = re.split(f"(?<=[{SENTENCE_END}\\n])", text)
    chunks, cur = [], ""
    for raw in parts:
        p = raw.strip()
        if not p:
            continue
        if len(p) > max_chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_hard_split(p, max_chunk))
            continue
        if cur and len(cur) + len(p) > MERGE_UPTO:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def main() -> None:
    t_start = time.time()
    log("[init] 加载 Piper 模型...")
    try:
        from piper import PiperVoice
    except ImportError:
        log("[error] 未安装 piper-tts，请先执行 setup_jet.sh")
        sys.exit(2)
    if not os.path.exists(MODEL_PATH):
        log(f"[error] 找不到语音模型: {MODEL_PATH}，请先执行 setup_jet.sh")
        sys.exit(2)

    voice = PiperVoice.load(MODEL_PATH)
    sample_rate = int(getattr(getattr(voice, "config", None), "sample_rate", SAMPLE_RATE_FALLBACK))
    log(f"[init] 模型就绪 sample_rate={sample_rate}")

    # 新版(1.8+)使用 SynthesisConfig；旧版(1.x)走流式原始 API
    try:
        from piper.config import SynthesisConfig
    except ImportError:
        SynthesisConfig = None

    # 整篇读入（万字长文也没问题），切块后逐段合成、逐段输出
    text = sys.stdin.buffer.read().decode("utf-8")
    chunks = split_text(text)
    if not chunks:
        log("[error] 文本为空，没有可合成的内容")
        sys.exit(1)
    log(f"[init] 共 {len(chunks)} 段，总字数 {len(text)}，开始流式合成")

    total_audio = 0.0
    dump_f = open(os.path.expanduser("~/jetson-tts/out.pcm"), "wb")  # 远端同步落盘，Mac 播完拉走保存

    def emit(piece):
        sys.stdout.buffer.write(piece)
        sys.stdout.buffer.flush()
        dump_f.write(piece)
        return len(piece)

    try:
        for i, ch in enumerate(chunks, 1):
            t_seg = time.time()
            n_bytes = 0
            if SynthesisConfig is not None:
                # piper-tts >= 1.8：synthesize 本身返回按句产出的音频流
                for ac in voice.synthesize(ch, SynthesisConfig()):
                    n_bytes += emit(ac.audio_int16_bytes)
            else:
                # 旧版：优先使用流式 API（逐帧产出），再回退整段合成
                try:
                    gen = voice.synthesize_stream_raw(ch, sentence_silence=0.25)
                except AttributeError:
                    gen = iter([voice.synthesize_raw(ch)])
                except TypeError:
                    gen = voice.synthesize_stream_raw(ch)
                for piece in gen:
                    n_bytes += emit(piece)

            dur = n_bytes / 2 / sample_rate
            total_audio += dur
            log(f"[{i}/{len(chunks)}] {len(ch)}字 -> 音频{dur:.1f}s (合成耗时{time.time()-t_seg:.1f}s)")
    except BrokenPipeError:
        log("[info] 输出管道已关闭，停止合成（已合成的音频保留在远端）")
        dump_f.close()
        sys.exit(0)

    log(f"[done] 音频总时长 {total_audio:.1f}s，Jetson 总耗时 {time.time()-t_start:.1f}s")
    dump_f.flush()
    dump_f.close()
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()