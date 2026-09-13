#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac 端极简流式播放器：从 stdin 读 PCM(int16 单声道)，按到达顺序原样播放。
无时钟追赶、无丢帧 —— 替代 ffplay，根治"开头语音被跳过"问题。
用法: speak_player.py <sample_rate>
"""
import sys

import sounddevice as sd


def main() -> None:
    sr = int(sys.argv[1]) if len(sys.argv) > 1 else 32000
    stream = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
    with stream:
        while True:
            data = sys.stdin.buffer.read(8192)
            if not data:
                break
            stream.write(data)  # 阻塞写入，保证按到达顺序完整播出


if __name__ == "__main__":
    main()