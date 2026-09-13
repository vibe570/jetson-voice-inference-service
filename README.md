# Mac → Jetson 流式语音合成(局域网服务)

Mac 上传长文(支持万字)→ Jetson 边推理边分段合成 → 音频流式回传 Mac 实时播放,并同步保存 wav。

- **默认音色**:Piper 神经网络 TTS(极速,RTF≈0.1,支持万字连续朗读)
- **克隆音色**:调用 Jetson 上已部署的 GPT-SoVITS api_v2 服务,上传 5~10 秒参考音频即可克隆,之后所有合成默认使用该音色,直到再次切换

## 架构

```
Mac(client)                                Jetson
┌─────────────────────────┐    SSH(免密)    ┌──────────────────────────────┐
│ client/speak.sh         │ ──────────────▶ │ jetson/setup_jet.sh  初始化   │
│  ├─ 文本/md 清洗         │                 │ jetson/piper_stream.py       │
│  ├─ 引擎/音色切换        │                 │ jetson/clone_stream.py 伪流式 │
│  └─ 保存 voice/*.wav     │ ◀────────────── │           └─ POST /tts       │
│ client/speak_player.py  │   PCM 流 (32000/ │ GPT-SoVITS api_v2 (端口 9880) │
│  sounddevice 按序播放    │   22050 Hz s16le)│ jetson/sovits/ 内为部署文件   │
└─────────────────────────┘                 └──────────────────────────────┘
```

## 目录结构

```
client/                  # Mac 端(局域网客户端)
  speak.sh               入口: 引擎/音色切换、md 清洗、自动保存、服务自愈
  speak_player.py        sounddevice 流式播放器(无时钟追赶,不丢开头)
jetson/                  # Jetson 端(推理服务)
  clone_stream.py        参考音频克隆, 伪流式(分段整段合成+双缓冲流水线)
  piper_stream.py        Piper 万字段落流式合成
  setup_jet.sh           一键初始化 Piper 环境(venv + 模型下载)
  sovits/                GPT-SoVITS 服务部署(从 0 安装用)
    install_sovits.sh    从 0 部署脚本(源码+torch+依赖+预训练模型)
    api_v2.py            API 服务源码(当前运行版本)
    tts_infer.yaml       配置(默认节: v2ProPlus + zizi1 微调权重, 需自备权重)
    tts_infer.vanilla.yaml 配置(官方预训练 v2ProPlus + CUDA, 无需自备权重)
speak.sh                 → client/speak.sh 软链(仓库根入口)
```

---

# 部署:两台设备从 0 复现

前提: Mac(任意,有 Python3)与 Jetson(已刷 JetPack,建议 6.x / Orin 系列)在同一局域网。

## 第一步: Jetson 端(一次性,约 30 分钟)

### 1.1 克隆本仓库

```bash
git clone https://github.com/vibe570/GPT-SoVITS-Jetson.git ~/GPT-SoVITS-Jetson
```

### 1.2 部署 GPT-SoVITS 推理服务(从 0)

`jetson/sovits/install_sovits.sh` 会完成:官方源码(pin 到已验证的 commit)→ CUDA 版 PyTorch → 官方依赖 → 官方预训练模型(hf-mirror 镜像,海外机器可改用官方源)→ 放置 api_v2.py 与 vanilla 配置。

```bash
cd ~/GPT-SoVITS-Jetson/jetson/sovits
bash install_sovits.sh 你的sudo密码          # 约 30 分钟(下载 torch + 2 个中文预训练模型)
```

启动服务并验证:

```bash
cd ~/GPT-SoVITS
nohup ./sovits-venv/bin/python api_v2.py -a None -p 9880 \
    -c GPT_SoVITS/configs/tts_infer.yaml >> api.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9880/   # 输出 404 = 服务已就绪
```

> - 服务重启后不会自启,但 speak.sh 每次运行会自动检测并拉起(等待加载约 1~2 分钟),无需手动操作
> - 已有自训练权重(如 zizi1 微调):将 ckpt/pth 放入对应目录后改用 `tts_infer.yaml`(默认节),否则用 vanilla 预训练音色完全跟随参考音频
> - 官方预训练模型较大(chinese-roberta-wwm-ext-large 约 1.3GB),耐心等待

### 1.3 zram 内存保护(强烈建议)

GPT-SoVITS 服务常驻约 5.6GB 内存。8GB 内存机型请确认 zram 生效(重启后可能因模块未加载而失败):

```bash
sudo modprobe zram && sudo systemctl restart zram-swap
swapon --show                    # 应显示 /dev/zram0 4GB
echo zram | sudo tee /etc/modules-load.d/zram.conf   # 持久化,防止重启后再失效
```

### 1.4 初始化本仓库的 Piper 环境

```bash
cd ~/GPT-SoVITS-Jetson/jetson
bash setup_jet.sh 你的sudo密码     # venv + piper-tts + zh_CN-huayan-medium 语音模型
```

(这一步也会在 Mac 首次运行 speak.sh 时自动完成,MAC 侧会自动 scp 该脚本上去跑。)

## 第二步: Mac 端(一次性,约 3 分钟)

```bash
git clone https://github.com/vibe570/GPT-SoVITS-Jetson.git
cd GPT-SoVITS-Jetson

# 可选:安装 ffplay(备用播放器)与 sshpass(仅首次安装免密密钥时用)
brew install ffmpeg hudochenkov/sshpass/sshpass

# 首次运行:自动创建本机播放器环境(venv+sounddevice)、生成并安装 SSH 免密密钥、
# 同步脚本到 Jetson、触发 Jetson 端 Piper 初始化(如未做)
JET_HOST=你的jetson地址 JET_PASSWORD=你的密码 ./speak.sh "你好,部署成功。"
```

> `JET_HOST` 默认 `jet`:可直接写入 `~/.ssh/config`(`Host jet / HostName 设备IP / User 用户名`),之后免填。

## 第三步: 设置参考音频(克隆音色,可选)

```bash
./speak.sh --set-ref 你的录音.wav "录音对应的文字"   # 5~10 秒清晰人声最佳
./speak.sh --clear-ref                              # 切回 Piper 默认音色
```

---

# 日常使用

```bash
./speak.sh 文章.txt               # 发文本(txt/md 均可)
cat 文章.txt | ./speak.sh         # 或走 stdin
```

- `.md` 自动清洗 Markdown 标记(只读正文);`txt` 原样发送
- 每次运行同步保存 wav 到 **当前目录 voice/**(`日期-时间-进程号.wav`;Piper 22.05kHz / 克隆 32kHz)
- 音色状态持久化在 Jetson:`--set-ref` 之后所有调用默认克隆音色,直到 `--clear-ref` 或再次 `--set-ref`

---

# 关键设计(踩坑记录)

- **伪流式**:GPT-SoVITS 服务端 streaming_mode 输出已被验证有明显杂音,整段合成则正常;且整段请求会截断长文本。因此克隆端采用"***按句切段 → 每段独立整段合成 → 双缓冲流水线连续输出***",音质完整且保持流式体验
- **batch 并行**:每请求合并约 120 字 + batch_size=8,RTF 约 0.67(合成领先播放,不卡顿),音色稳定
- **播放器**:ffplay 在预缓冲空等后出现"开头丢音"(内部时钟追赶),已改用 sounddevice 阻塞写播放器(按到达顺序,不丢不重排),ffplay 仅作回退
- **播放与保存解耦**:Jetson 端同步落盘 PCM,Mac 播完拉回转 wav;Ctrl+C 不丢已合成部分
- **健壮性**:单段失败重试 3 次后跳过继续(不吞剩余文本);语速偏快段只警告不丢段;单段超时 240s、流水线假死检测 600s
- **服务自愈**:9880 未响应自动拉起;zram 修复见 1.3

# 当前进度

已完成:

- [x] Mac → Jetson SSH 免密链路(首次自动装密钥)
- [x] Piper 万字段落流式朗读(10k 字实测 96 段连续输出)
- [x] 参考音频克隆(复用已部署 GPT-SoVITS,--set-ref 持久化)
- [x] 伪流式: 分段合成+双缓冲流水线, 长文不丢段, 失败自动跳过续播
- [x] Markdown 清洗、自动保存 wav、Jetson 重启自愈
- [x] GPU 推理(CUDA fp16, batch=8, RTF≈0.67)+ MAXN Super 电源模式
- [x] 从 0 复现: Jetson install_sovits.sh 一键脚本 + 双端分步文档

已知限制:

- 克隆模式合成速度约 1 倍实时,首声需等预合成(约 10~30 秒)
- 参考音频质量决定克隆音色上限(人声分离切片可能带噪声)
- 自训练权重(约 320MB)不随仓库分发,需自备;无权重时走 vanilla 预训练配置