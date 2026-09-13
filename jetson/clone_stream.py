#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson 端：GPT-SoVITS 分段合成 + 连续流式输出（"伪流式"）
- 服务端 streaming_mode 输出有杂音（整段合成正常），故放弃服务端流式
- 长文按句切段，每段一次独立的非流式整段合成（质量稳定）
- 合成线程与播放完全解耦：合成线程全速推理并直接落盘 out.pcm；
  主线程从磁盘缓冲追读、以任何速度写 stdout（不受合成速率影响）
- 全部合成完成的瞬间就在 Jetson 端生成 out.wav 并写 out.done 标记，
  Mac 端 watcher 可立即拉回 wav，无需等播放结束
"""
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import wave

API_BASE = os.environ.get("SOVITS_API", "http://127.0.0.1:9880")
REF_DIR = os.path.expanduser("~/jetson-tts/ref")
REF_WAV = os.path.join(REF_DIR, "reference.wav")
REF_TEXT_FILE = os.path.join(REF_DIR, "reference.txt")
SAMPLE_RATE = 32000  # GPT-SoVITS 输出固定 32kHz
DUMP_PATH = os.path.expanduser("~/jetson-tts/out.pcm")   # 原始 PCM 缓冲（合成边写）
OUT_WAV_PATH = os.path.expanduser("~/jetson-tts/out.wav")  # 合成完成即生成的完整 wav
DONE_PATH = os.path.expanduser("~/jetson-tts/out.done")  # wav 就绪标记，Mac 侧据此即时拉取

SENTENCE_END = "。！？!?…；;"
SOFT_DELIM = "，,、：:"
MAX_CHUNK = int(os.environ.get("SOVITS_CHUNK", "120"))  # 每次请求合并的最大字数（可用环境变量覆盖做实验）
BATCH_SIZE = int(os.environ.get("SOVITS_BATCH", "8"))   # 服务端批量并行推理（可用环境变量覆盖做实验）
RETRY = 3            # 每段失败的重试次数
FASTEST = 10.0       # 允许的最快语速（字/秒）：低于此值只警告不丢段；真正截断(丢句)才会超过
PAD_MS = 60          # 段间静音垫（毫秒），仅做自然句读停顿
HTTP_TIMEOUT = 240   # 单段请求超时（秒）：避免 Jetson 内存高压下请求挂起导致假死


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _hard_split(p: str, max_chunk: int):
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
        if cur and len(cur) + len(p) > max_chunk + 10:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def synth_one(text: str, ref_wav: str, prompt_text: str):
    """一次非流式整段合成，返回 (pcm_bytes, 音频时长秒, 本次网络请求耗时秒)。"""
    payload = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": ref_wav,
        "prompt_text": prompt_text,
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": BATCH_SIZE,
        "media_type": "wav",
        "streaming_mode": 0,  # 不用服务端流式（输出有杂音），整段质量稳定
        "speed_factor": 1.0,
    }
    req = urllib.request.Request(
        f"{API_BASE}/tts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()  # 精确计时：urlopen 到 read 完成（纯推理+网络往返）
    try:
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
            msg = detail.get("message") or detail.get("detail") or str(detail)
        except Exception:
            msg = f"HTTP {e.code}"
        raise RuntimeError(f"接口错误({e.code}): {msg}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接服务 {API_BASE}: {e.reason}") from None

    body = resp.read()
    req_time = time.time() - t0
    if len(body) < 44 or body[:4] != b"RIFF":
        raise RuntimeError("响应不是完整 wav")
    pcm = body[44:]
    return pcm, len(pcm) / 2 / SAMPLE_RATE, req_time


def finalize_wav():
    """把完整 out.pcm 加上 wav 头转成 out.wav，并写 done 标记。"""
    tmp = OUT_WAV_PATH + ".tmp"
    with open(DUMP_PATH, "rb") as src, wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        while True:
            buf = src.read(1 << 20)
            if not buf:
                break
            w.writeframes(buf)
    os.rename(tmp, OUT_WAV_PATH)
    with open(DONE_PATH, "w") as f:
        f.write(str(os.path.getsize(OUT_WAV_PATH)))


def main() -> None:
    t_start = time.time()
    if not (os.path.exists(REF_WAV) and os.path.exists(REF_TEXT_FILE)):
        log("[error] 未设置参考音频，请先在 Mac 端执行: ./speak.sh --set-ref 参考音频.wav '参考音频对应文字'")
        sys.exit(2)
    prompt_text = open(REF_TEXT_FILE, encoding="utf-8").read().strip()
    if not prompt_text:
        log("[error] 参考音频文字为空，请用 --set-ref 重新设置")
        sys.exit(2)

    log(f"[init] 参考音频: {REF_WAV}，合成与播放解耦流水线")

    text = sys.stdin.buffer.read().decode("utf-8")
    chunks = split_text(text)
    if not chunks:
        log("[error] 文本为空，没有可合成的内容")
        sys.exit(1)
    log(f"[init] 共 {len(chunks)} 段，总字数 {len(text)}，推理进度与播放进度分开显示")

    pad = b"\x00\x00" * int(SAMPLE_RATE * PAD_MS / 1000)  # 段间静音垫
    dump_w = open(DUMP_PATH, "wb")  # 合成线程直写（顺序 = 文本顺序）
    # 通知队列：只传元数据（段号/字节数），PCM 走磁盘缓冲，内存 O(1)
    q: "queue.Queue" = queue.Queue()
    compute_holder = {"total": 0.0, "requests": 0}

    def producer():
        for i, ch in enumerate(chunks):
            last_pcm = last_dur = last_dt = None
            last_err = ""
            for attempt in range(RETRY + 1):
                try:
                    pcm, dur, dt = synth_one(ch, REF_WAV, prompt_text)
                    compute_holder["total"] += dt
                    compute_holder["requests"] += 1
                    last_pcm, last_dur, last_dt = pcm, dur, dt
                    if dur >= len(ch) / FASTEST:
                        break
                    last_err = f"疑似偏快({dur:.1f}s/{len(ch)}字)"
                    log(f"[warn] 第{i+1}段 {last_err}，重试 {attempt+1}/{RETRY}")
                except Exception as e:
                    last_err = str(e)
                    log(f"[warn] 第{i+1}段失败: {e}，重试 {attempt+1}/{RETRY}")
                    time.sleep(2)
            else:
                # 重试耗尽：拿到过音频就照播（偏快只是警告，绝不丢段）；彻底失败才跳过
                last_err = f"[注意] {last_err}" if last_pcm else f"合成失败，已跳过: {ch[:20]}…"

            if last_pcm:
                seg = (pad if i > 0 else b"") + last_pcm
                dump_w.write(seg)
                dump_w.flush()
                q.put((i, len(seg), last_dur, last_dt, last_err))
                log(f"[推理 {i+1}/{len(chunks)}] {len(ch)}字 耗时{last_dt:.1f}s")
            else:
                q.put((i, 0, 0.0, 0.0, last_err))
                log(f"[推理 {i+1}/{len(chunks)}] 跳过")

        dump_w.flush()
        dump_w.close()
        try:
            finalize_wav()
            log(f"[synth-ready] 全部合成完成，wav 已生成: {OUT_WAV_PATH} (compute {compute_holder['total']:.1f}s / {compute_holder['requests']}次请求)")
        except Exception as e:
            log(f"[warn] wav 转换失败: {e}")
        q.put(None)

    threading.Thread(target=producer, daemon=True).start()

    dump_r = open(DUMP_PATH, "rb")
    read_pos = 0
    total_audio = 0.0

    def read_seg(nbytes: int) -> bytes:
        nonlocal read_pos
        data = b""
        while len(data) < nbytes:
            dump_r.seek(read_pos)
            piece = dump_r.read(nbytes - len(data))
            if not piece:
                time.sleep(0.2)  # 合成未写完该段，短暂等待
                continue
            read_pos += len(piece)
            data += piece
        return data

    try:
        for _ in range(len(chunks)):
            item = q.get(timeout=600)  # 长时间无段产出说明服务假死，超时退出不再傻等
            if item is None:
                break
            i, seg_len, dur, dt, status = item
            if seg_len == 0:
                log(f"[skip] 第{i+1}段 {status}")
                continue
            data = read_seg(seg_len)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            total_audio += dur
            log(f"[播放 {i+1}/{len(chunks)}] 段长{seg_len//64}KB -> 音频{dur:.1f}s")
        # 播放完毕后等待合成线程收尾（生成 out.wav + out.done 标记）
        while True:
            item = q.get(timeout=600)
            if item is None:
                break
    except queue.Empty:
        log("[error] 服务长时间无响应（600s），已放弃后续段落")
    except BrokenPipeError:
        # Mac 端主动中断（Ctrl+C 或播放器退出）
        log("[info] 输出管道已关闭，停止播放（远端数据与 wav 产物不受影响）")
        sys.exit(0)

    wall_time = time.time() - t_start
    wav_note = "wav 已生成" if os.path.exists(DONE_PATH) else "wav 未生成"
    log(
        f"[done] 音频总时长(播放) {total_audio:.1f}s | "
        f"TTS合成耗时(compute) {compute_holder['total']:.1f}s "
        f"({compute_holder['requests']}次请求) | "
        f"流程总耗时(wall) {wall_time:.1f}s | {wav_note}"
    )
    dump_r.close()
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()