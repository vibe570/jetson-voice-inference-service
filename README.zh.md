# Jetson 语音推理服务

> [English](README.md)

Mac 通过局域网向 Jetson 发送长文(支持万字),Jetson 本地推理、边合成边把音频流式回传 Mac 播放,并同步保存 wav。

- **默认音色**:Piper 神经网络 TTS(极速,RTF≈0.1,万字连续朗读)
- **克隆音色**:调用 Jetson 上已部署的 GPT-SoVITS api_v2 服务,提供 5~10 秒参考音频即可克隆,此后所有合成默认使用该音色,直到再次切换

## 架构

```
Mac(client)                                Jetson
┌─────────────────────────┐    SSH(免密)    ┌──────────────────────────────┐
│ client/speak.sh         │ ──────────────▶ │ jetson/setup_jet.sh  初始化   │
│  ├─ 文本/md 清洗         │                 │ jetson/piper_stream.py       │
│  ├─ 引擎/音色切换        │                 │ jetson/clone_stream.py 伪流式 │
│  └─ 保存 voice/*.wav     │ ◀────────────── │           └─ POST /tts       │
│ client/speak_player.py  │   PCM 流 (32000/ │ GPT-SoVITS api_v2 (端口 9880) │
│  sounddevice 按序播放    │   22050 Hz s16le)│ 部署文件在 jetson/sovits/     │
└─────────────────────────┘                 └──────────────────────────────┘
```

## 目录结构

```
client/                  # Mac 端(局域网客户端)
  speak.sh               入口: 引擎/音色切换、md 清洗、自动保存、服务自愈
  speak_player.py        sounddevice 流式播放器
jetson/                  # Jetson 端(推理)
  clone_stream.py        参考音频克隆, 伪流式(分段整段合成+双缓冲流水线)
  piper_stream.py        Piper 万字段落流式合成
  setup_jet.sh           一键初始化 Piper 环境
  sovits/                GPT-SoVITS 服务部署(从 0)
    install_sovits.sh    从 0 部署(源码+CUDA torch+依赖+预训练模型)
    api_v2.py            API 服务源码(当前运行版本)
    tts_infer.yaml       配置(默认节: v2ProPlus + 自训练权重)
    tts_infer.vanilla.yaml 配置(官方预训练 v2ProPlus + CUDA)
speak.sh                 → client/speak.sh 软链(根入口)
```

---

# 部署:两台设备从 0 复现

前提: Mac(任意,有 Python3)与 Jetson(已刷 JetPack,建议 6.x / Orin 系列)在同一局域网。

## 第一步: Jetson 端(一次性,约 30 分钟)

### 1.1 克隆本仓库

```bash
git clone https://github.com/vibe570/jetson-voice-inference-service.git ~/jetson-voice-inference-service
```

### 1.2 部署 GPT-SoVITS 推理服务(从 0)

`jetson/sovits/install_sovits.sh` 自动完成:官方源码(固定已验证提交)→ CUDA 版 PyTorch → 官方依赖 → 官方预训练模型(hf-mirror 镜像,海外可去掉)→ 放置 api_v2.py 与 vanilla 配置。

```bash
cd ~/jetson-voice-inference-service/jetson/sovits
bash install_sovits.sh 你的sudo密码          # 约 30 分钟
```

启动服务并验证:

```bash
cd ~/GPT-SoVITS
nohup ./sovits-venv/bin/python api_v2.py -a None -p 9880 \
    -c GPT_SoVITS/configs/tts_infer.yaml >> api.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9880/   # 输出 404 = 服务已就绪
```

> - 服务重启后不会自启,但 speak.sh 每次运行会自动检测并拉起(等待加载约 1~2 分钟)
> - 有自训练权重:放入对应目录后改用 `tts_infer.yaml`;没有则用 vanilla 预训练配置,音色完全跟随参考音频

### 1.3 zram 内存保护(强烈建议)

服务常驻约 5.6GB 内存,8GB 机型务必确认 zram 生效(重启后可能因模块未加载而丢失):

```bash
sudo modprobe zram && sudo systemctl restart zram-swap
swapon --show                    # 应显示 /dev/zram0 4GB
echo zram | sudo tee /etc/modules-load.d/zram.conf   # 持久化
```

### 1.4 初始化 Piper 环境

```bash
cd ~/jetson-voice-inference-service/jetson
bash setup_jet.sh 你的sudo密码
```

(Mac 首次运行 speak.sh 时也会自动完成此步。)

## 第二步: Mac 端(一次性,约 3 分钟)

```bash
git clone https://github.com/vibe570/jetson-voice-inference-service.git
cd jetson-voice-inference-service

# 可选:ffplay 备用播放器、sshpass 仅首次安装密钥时用
brew install ffmpeg hudochenkov/sshpass/sshpass

# 首次运行:自动创建本地播放器环境(venv+sounddevice)、安装 SSH 免密密钥、同步脚本、触发 Jetson 初始化
JET_HOST=你的jetson地址 JET_PASSWORD=你的密码 ./speak.sh "你好,部署成功。"
```

> `JET_HOST` 默认 `jet`:可写入 `~/.ssh/config`(`Host jet / HostName 设备IP / User 用户名`)后免填。

## 第三步: 设置参考音频(克隆音色,可选)

```bash
./speak.sh --set-ref 你的录音.wav "录音对应的文字"   # 5~10 秒清晰人声最佳
./speak.sh --clear-ref                              # 切回 Piper
```

---

# 日常使用

```bash
./speak.sh 文章.txt               # txt/md 均可
cat 文章.txt | ./speak.sh         # 或走 stdin
```

- `.md` 自动清洗 Markdown 标记;`txt` 原样发送
- 每次运行同步保存 wav 到 **当前目录 voice/**(`日期-时间-进程号.wav`;Piper 22.05kHz / 克隆 32kHz)
- 音色状态持久化在 Jetson:`--set-ref` 后默认克隆音色,直到 `--clear-ref`