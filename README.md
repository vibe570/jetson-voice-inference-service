# Jetson Voice Inference Service

> [中文文档](README.zh.md)

Stream long-form text (up to tens of thousands of Chinese characters) from a Mac to a Jetson over LAN. The Jetson runs on-device inference and streams the audio back in chunks, so playback starts while synthesis is still in progress. Audio is also saved as WAV.

- **Default voice**: Piper neural TTS (extremely fast, RTF ≈ 0.1, handles 10k-character documents continuously)
- **Cloned voice**: uses the GPT-SoVITS api_v2 service already deployed on the Jetson. Provide a 5–10s reference audio and every synthesis afterwards uses that voice until you switch again.

## Architecture

```
Mac (client)                                 Jetson
┌──────────────────────────┐    SSH (keyless)  ┌────────────────────────────────┐
│ client/speak.sh          │ ────────────────▶ │ jetson/setup_jet.sh     init   │
│  ├─ txt/md cleaning      │                   │ jetson/piper_stream.py  Piper   │
│  ├─ engine/voice switch  │                   │ jetson/clone_stream.py  clone   │
│  └─ save voice/*.wav     │ ◀──────────────── │            └─ POST /tts        │
│ client/speak_player.py   │  PCM stream       │ GPT-SoVITS api_v2 (port 9880)  │
│  sounddevice playback    │  (32000/22050 Hz) │ deployment files in jetson/sovits│
└──────────────────────────┘                   └────────────────────────────────┘
```

## Repository layout

```
client/                  # Mac side (LAN client)
  speak.sh               entry point: engine/voice switching, md cleaning, auto-save
  speak_player.py        sounddevice streaming player (no clock-chasing, no dropped head)
jetson/                  # Jetson side (inference)
  clone_stream.py        reference-audio cloning, pseudo-streaming (per-chunk + pipeline)
  piper_stream.py        Piper streaming synthesis for long documents
  setup_jet.sh           one-shot Piper environment setup (venv + model download)
  sovits/                GPT-SoVITS service deployment (from scratch)
    install_sovits.sh      one-shot deploy: source + CUDA torch + deps + pretrained models
    api_v2.py              API service source (the running version)
    tts_infer.yaml         config (default section: v2ProPlus + self-trained weights)
    tts_infer.vanilla.yaml config (official pretrained v2ProPlus + CUDA, no extra weights)
speak.sh                 symlink → client/speak.sh (repo-root entry point)
```

---

# Deployment: from zero on both machines

Prerequisites: any Mac with Python 3, and a Jetson flashed with JetPack (6.x recommended, Orin family), both on the same LAN.

## Step 1 — Jetson (one-time, ~30 min)

### 1.1 Clone this repository

```bash
git clone https://github.com/vibe570/jetson-voice-inference-service.git ~/jetson-voice-inference-service
```

### 1.2 Deploy the GPT-SoVITS inference service (from scratch)

`jetson/sovits/install_sovits.sh` does: official source (pinned commit) → CUDA PyTorch → official requirements → official pretrained models (hf-mirror; remove mirror for machines outside China) → install `api_v2.py` and the vanilla config.

```bash
cd ~/jetson-voice-inference-service/jetson/sovits
bash install_sovits.sh <your-sudo-password>   # ~30 min (torch + two Chinese pretrained models)
```

Start the service and verify:

```bash
cd ~/GPT-SoVITS
nohup ./sovits-venv/bin/python api_v2.py -a None -p 9880 \
    -c GPT_SoVITS/configs/tts_infer.yaml >> api.log 2>&1 &
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9880/   # 404 = server is up
```

> - The service does not auto-start after a reboot, but `speak.sh` detects this and starts it automatically (waiting 1–2 minutes for model load).
> - If you have self-trained weights: place the ckpt/pth files and use `tts_infer.yaml`; otherwise the vanilla pretrained config is used and the voice follows the reference audio.

### 1.3 zram memory protection (strongly recommended)

The GPT-SoVITS service holds ~5.6GB resident. On 8GB Jetsons make sure the 4GB zram is active (it can silently fail after reboot):

```bash
sudo modprobe zram && sudo systemctl restart zram-swap
swapon --show                    # should list /dev/zram0 4GB
echo zram | sudo tee /etc/modules-load.d/zram.conf   # persist across reboots
```

### 1.4 Initialize the Piper environment

```bash
cd ~/jetson-voice-inference-service/jetson
bash setup_jet.sh <your-sudo-password>   # venv + piper-tts + zh_CN-huayan-medium voice model
```

(This step is also performed automatically by `speak.sh` on first run from the Mac.)

## Step 2 — Mac (one-time, ~3 min)

```bash
git clone https://github.com/vibe570/jetson-voice-inference-service.git
cd jetson-voice-inference-service

# optional: ffplay fallback player, sshpass only for first-time key install
brew install ffmpeg hudochenkov/sshpass/sshpass

# first run: creates local player venv (sounddevice), generates & installs the SSH key,
# syncs scripts to Jetson, triggers Piper init if missing
JET_HOST=<jetson-host> JET_PASSWORD=<password> ./speak.sh "Hello, deployment works."
```

> `JET_HOST` defaults to `jet`; add it to `~/.ssh/config` (`Host jet / HostName <IP> / User <name>`) to skip the env vars.

## Step 3 — Set a reference audio (cloned voice, optional)

```bash
./speak.sh --set-ref your-recording.wav "the text spoken in the recording"   # 5–10s clean speech works best
./speak.sh --clear-ref                                                       # back to Piper
```

---

# Usage

```bash
./speak.sh article.txt        # txt or md
cat article.txt | ./speak.sh  # or via stdin
```

- `.md` files are cleaned of Markdown syntax automatically; `txt` is sent verbatim.
- Every run also saves a WAV to `voice/` in the current directory (`date-time-pid.wav`, Piper 22.05kHz / clone 32kHz).
- Voice selection persists on the Jetson: after `--set-ref` all runs use the cloned voice until `--clear-ref`.