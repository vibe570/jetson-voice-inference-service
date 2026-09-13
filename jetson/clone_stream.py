#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jetson 端：GPT-SoVITS 分段合成 + 连续流式输出（"伪流式"）
- 服务端 streaming_mode 输出有杂音（整段合成正常），故放弃服务端流式
- 长文按句切段，每段一次独立的非流式整段合成（质量稳定）
- 双缓冲流水线：预合成 2 段后开始输出，边播边合成后续段，段间加 200ms 静音垫
- 单段失败（接口错误/音频异常短）重试后跳过继续，绝不让一段失败吞掉剩余文本
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

API_BASE = os.environ.get("SOVITS_API", "http://127.0.0.1:9880")
REF_DIR = os.path.expanduser("~/jetson-tts/ref")
REF_WAV = os.path.join(REF_DIR, "reference.wav")
REF_TEXT_FILE = os.path.join(REF_DIR, "reference.txt")
SAMPLE_RATE = 32000  # GPT-SoVITS 输出固定 32kHz

SENTENCE_END = "。！？!?…；;"
SOFT_DELIM = "，,、：:"
MAX_CHUNK = 120      # 每次请求合并的最大字数（用户验证该配置音色正常，勿改动）
BATCH_SIZE = 8       # 服务端批量并行推理（用户验证音色正常且吞吐高，勿改动）
RETRY = 3            # 每段失败的重试次数
FASTEST = 10.0       # 允许的最快语速（字/秒）：低于此值只警告不丢段；真正截断(丢句)才会超过
PREBUFFER = 3        # 开始播放前预合成的批次数（合成领先越多，越不容易卡顿）
PAD_MS = 60          # 段间静音垫（毫秒），仅做自然句读停顿
HTTP_TIMEOUT = 240   # 单段请求超时（秒）：避免 Jetson 内存高压下请求挂起导致假死
DUMP_PATH = os.path.expanduser("~/jetson-tts/out.pcm")  # 同时在远端落盘，供 Mac 播完后拉取保存


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


def main() -> None:
    t_start = time.time()
    if not (os.path.exists(REF_WAV) and os.path.exists(REF_TEXT_FILE)):
        log("[error] 未设置参考音频，请先在 Mac 端执行: ./speak.sh --set-ref 参考音频.wav '参考音频对应文字'")
        sys.exit(2)
    prompt_text = open(REF_TEXT_FILE, encoding="utf-8").read().strip()
    if not prompt_text:
        log("[error] 参考音频文字为空，请用 --set-ref 重新设置")
        sys.exit(2)

    log(f"[init] 参考音频: {REF_WAV}，分段整段合成流水线（伪流式）")

    text = sys.stdin.buffer.read().decode("utf-8")
    chunks = split_text(text)
    if not chunks:
        log("[error] 文本为空，没有可合成的内容")
        sys.exit(1)
    log(f"[init] 共 {len(chunks)} 段，总字数 {len(text)}，预合成 {PREBUFFER} 段后开始连续播放")

    # 双缓冲流水线：合成线程逐段合成入队；单段失败重试后跳过，绝不中断后续段落
    q: "queue.Queue" = queue.Queue(maxsize=PREBUFFER + 2)
    compute_holder = {"total": 0.0, "requests": 0}  # 纯推理耗时 = 每次网络请求耗时累加

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
                        q.put((i, pcm, dur, dt, ""))
                        break
                    last_err = f"疑似偏快({dur:.1f}s/{len(ch)}字)"
                    log(f"[warn] 第{i+1}段 {last_err}，重试 {attempt+1}/{RETRY}")
                except Exception as e:
                    last_err = str(e)
                    log(f"[warn] 第{i+1}段失败: {e}，重试 {attempt+1}/{RETRY}")
                    time.sleep(2)
            else:
                # 重试耗尽：拿到过音频就照播（偏快只是警告，绝不丢段）；彻底失败才跳过
                if last_pcm:
                    q.put((i, last_pcm, last_dur, last_dt, f"[注意] {last_err}"))
                else:
                    q.put((i, b"", 0.0, 0.0, f"合成失败，已跳过: {ch[:20]}…"))
        q.put(None)

    threading.Thread(target=producer, daemon=True).start()

    pad = b"\x00\x00" * int(SAMPLE_RATE * PAD_MS / 1000)  # 段间静音垫
    dump_f = open(DUMP_PATH, "wb")  # 远端落盘同步保存（Mac 播完拉走转 wav）

    total_audio = 0.0
    wrote = False

    def play_item(item):
        nonlocal total_audio, wrote
        i, pcm, dur, t_synth, status = item
        if not pcm:
            log(f"[skip] 第{i+1}段 {status}")
            return
        if wrote:  # 段与段之间的静音垫，让衔接更自然
            sys.stdout.buffer.write(pad)
            dump_f.write(pad)
        sys.stdout.buffer.write(pcm)
        dump_f.write(pcm)
        sys.stdout.buffer.flush()
        dump_f.flush()
        wrote = True
        total_audio += dur
        log(f"[{i+1}/{len(chunks)}] {len(chunks[i])}字 -> 音频{dur:.1f}s (合成耗时{t_synth:.1f}s) {status}")

    # 预缓冲：先等前 PREBUFFER 段合成好，再开始输出（留出抗抖动余量）
    pending = []
    for _ in range(min(PREBUFFER, len(chunks))):
        item = q.get()
        if item is None:
            pending = None
            break
        pending.append(item)
    if pending is None:
        log("[error] 合成线程提前结束")
        sys.exit(1)

    try:
        for item in pending:
            play_item(item)

        while True:
            item = q.get(timeout=600)  # 长时间无段产出说明服务假死，超时退出不再傻等
            if item is None:
                break
            play_item(item)
    except queue.Empty:
        log("[error] 服务长时间无响应（600s），已放弃后续段落")
    except BrokenPipeError:
        # Mac 端主动中断（Ctrl+C 或播放器退出）：不再继续合成
        log("[info] 输出管道已关闭，停止合成（已合成的音频保留在远端）")
        dump_f.close()
        sys.exit(0)

    wall_time = time.time() - t_start
    log(
        f"[done] 音频总时长(播放) {total_audio:.1f}s | "
        f"TTS合成耗时(compute) {compute_holder['total']:.1f}s "
        f"({compute_holder['requests']}次请求) | "
        f"流程总耗时(wall) {wall_time:.1f}s"
    )
    dump_f.flush()
    dump_f.close()
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()