# Mac → Jetson 流式语音合成(局域网服务)

Mac 上传长文(支持万字)→ Jetson 边推理边分段合成 → 音频流式回传 Mac 实时播放,并同步保存 wav。

- **默认音色**:Piper 神经网络 TTS(极速,RTF≈0.1,支持万字连续朗读)
- **克隆音色**:调用 Jetson 上已部署的 GPT-SoVITS api_v2 服务,上传 5~10 秒参考音频即可克隆,之后所有合成默认使用该音色,直到再次切换

## 架构

```
Mac(client)                                Jetson(sky.local)
┌─────────────────────────┐    SSH(免密)    ┌──────────────────────────────┐
│ speak.sh                │ ──────────────▶ │ setup_jet.sh   环境初始化     │
│  ├─ 文本/md 清洗        │                 │ piper_stream.py  Piper 流式   │
│  ├─ 引擎/音色选择       │                 │ clone_stream.py  伪流式克隆   │
│  └─ 保存 voice/*.wav    │ ◀────────────── │              └─ POST /tts     │
│ speak_player.py         │   PCM 流(32000/ │ GPT-SoVITS api_v2(端口 9880)  │
│  sounddevice 按序播放   │   22050 Hz s16le)│   已有部署,常驻加载权重       │
└─────────────────────────┘                 └──────────────────────────────┘
```

## 结构

```
client/                  # Mac 端（局域网客户端）
  speak.sh               入口：引擎/音色切换、md 清洗、自动保存 wav
  speak_player.py        sounddevice 流式播放器
jetson/                  # Jetson 端（推理服务）
  clone_stream.py        参考音频克隆，伪流式（分段整段合成+双缓冲流水线）
  piper_stream.py        Piper 万字段落流式合成
  setup_jet.sh           一键环境初始化（venv + piper + 中文语音模型）
  sovits/                GPT-SoVITS 服务部署文件（当前在跑的版本）
    api_v2.py            API 服务源码（流式/非流式接口）
    tts_infer.yaml       模型配置（默认节: v2ProPlus + zizi1 微调权重）
speak.sh                 → client/speak.sh 软链（仓库根入口）
```

## 部署

### Jetson 端(一次性)

本仓库已在 Orin Nano Super 8GB / JetPack 6.2 上验证。

1. **部署 GPT-SoVITS 服务**(如果还没部署):

   ```bash
   # 安装完整的 GPT-SoVITS 环境后，把本仓库 jetson/sovits/ 下的部署文件放回对应位置：
   cp jetson/sovits/api_v2.py ~/GPT-SoVITS/
   cp jetson/sovits/tts_infer.yaml ~/GPT-SoVITS/GPT_SoVITS/configs/
   # 权重按 tts_infer.yaml 默认节放置（本项目用 v2ProPlus + zizi1 微调权重，约 320MB，不随仓库分发）：
   #   ~/GPT-SoVITS/GPT_weights_v2ProPlus/zizi1-e15.ckpt
   #   ~/GPT-SoVITS/SoVITS_weights_v2ProPlus/zizi1_e8_s816.pth
   cd ~/GPT-SoVITS
   ~/sovits-venv/bin/python api_v2.py -a None -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
   ```

   > 服务重启后不会自启,但 speak.sh 每次运行会自动检测并拉起(等待加载约 1~2 分钟),无需手动操作。

2. **初始化本仓库的 Jetson 端环境**(Piper + 语音模型):

   ```bash
   # 将本目录 scp 到 Jetson 任意位置后执行,或在 Mac 首次运行 speak.sh 时自动进行
   bash setup_jet.sh [sudo密码]
   ```

   会创建 `~/jetson-tts/`(venv、piper、zh_CN-huayan-medium 语音模型),以及 clone 模式的参考音频目录 `~/jetson-tts/ref/`。

### Mac 端

依赖:`sshpass`(仅首次装免密密钥)、`ffplay`(备选播放器)、Python3>=3.9(清洗用)。

```bash
# 首次运行会自动:生成并安装免密密钥(需 JET_PASSWORD)→ 同步脚本 → 初始化 Jetson 环境
./speak.sh 文章.txt
```

## 使用

```bash
./speak.sh 文章.txt                     # 发文本(txt/md 均可)
cat 文章.txt | ./speak.sh               # 或走 stdin
./speak.sh --set-ref 录音.wav "录音对应文字"   # 设置克隆音色(之后默认)
./speak.sh --clear-ref                  # 切回 Piper 默认音色
```

- `.md` 文件自动清洗 Markdown 标记(只读正文),`txt` 原样发送
- 每次运行音频自动保存到 **当前目录 voice/** 下(`日期-时间-进程号.wav`,Piper 22.05kHz / 克隆 32kHz)
- 环境变量:`JET_HOST`(默认 jet)、`JET_PASSWORD`(默认 1)

## 关键设计(踩坑记录)

- **伪流式**:GPT-SoVITS 服务端 streaming_mode 输出已被验证有明显杂音,整段合成则正常;且整段请求会截断长文本。因此 clone 端采用"***按句切段 → 每段独立整段合成 → 双缓冲流水线连续输出***",确保音质与完整性,同时保持流式体验
- **batch 并行**:每请求合并约 120 字 + batch_size=8,RTF 降至约 0.67(合成领先播放,不卡顿),且音色稳定
- **播放器**:ffplay 在预缓冲空等后会出现"开头丢音"(内部时钟追赶),已改用 sounddevice 回调式播放器(阻塞写、按到达顺序),ffplay 仅作回退
- **播放与保存解耦**:Jetson 端同步落盘 PCM,Mac 播完后拉回转换 wav;中途 Ctrl+C 不丢已合成部分
- **健壮性**:单段失败重试 3 次后跳过继续(绝不吞掉剩余文本);语速偏快段只警告不丢段;单段超时 240s、流水线假死检测 600s
- **zram**:Jetson 服务常驻约 5.6GB 内存,务必保持 4G zram 生效(曾因重启后 zram 模块未加载导致 OOM 风险;修复方式:`sudo modprobe zram && sudo systemctl restart zram-swap`,并写 `/etc/modules-load.d/zram.conf` 持久化)

## 当前进度

已完成:

- [x] Mac → Jetson SSH 免密链路(密钥自动安装)
- [x] Piper 万字段落流式朗读(10k 字实测 96 段连续输出,无卡顿)
- [x] 参考音频克隆:复用 Jetson 已部署 GPT-SoVITS,--set-ref 持久化音色、随时切换
- [x] 伪流式:分段合成 + 双缓冲流水线,长文不丢段、失败自动跳过续播
- [x] Markdown 自动清洗、自动保存 wav 到 voice/
- [x] Jetson 重启自愈:SoVITS 服务自动拉起、zram 恢复
- [x] GPU 推理(CUDA fp16,batch=8,RTF≈0.67)+ MAXN Super 电源模式

已知限制:

- 克隆模式合成速度约 1 倍实时(每段"合成耗时≈音频时长"),首声需等待预合成(约 10~30 秒)
- 参考音频质量决定克隆音色上限(人声分离切片可能带噪声)
- 保存功能在正常播完流程下拉取;Ctrl+C 中断时不保证 wav 已回传(远端 PCM 仍保留)