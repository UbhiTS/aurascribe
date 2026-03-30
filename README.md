# AuraScribe

Always-on meeting transcription with speaker identification, AI summaries, and Obsidian integration. Runs entirely locally on your GPU.

---

## What it does

- Listens via microphone all day — no manual start/stop required per meeting
- Identifies who is speaking (you vs. Person1, Person2, etc.)
- Transcribes in real time using Cohere Transcribe (2B, #1 English ASR) on your GPU
- Generates meeting summaries and action items via your local LLM (LM Studio)
- Writes everything to your Obsidian vault as linked markdown notes
- Drafts follow-up emails and meeting invites from any meeting
- Generates a Daily Brief aggregating all meetings, decisions, and priorities

---

## Requirements

- Linux with PipeWire or PulseAudio
- NVIDIA GPU with CUDA (tested on RTX 5090)
- Python 3.13 (`/usr/bin/python3.13`)
- Node.js 18+
- LM Studio running on your network with a model loaded
- A free HuggingFace account

---

## Step 1 — HuggingFace setup (do this first, before installing)

AuraScribe uses pyannote models for speaker diarization. They are free but require accepting a license on each model page.

**1a. Create a free account** at https://huggingface.co if you don't have one.

**1b. Create a read-only access token:**
- Go to https://huggingface.co → Settings → Access Tokens → New token
- Type: **Read** (or Fine-grained with read permissions)
- Copy the token — you'll need it in Step 3

**1c. Accept the license on ALL of these model pages** (while logged in, click "Agree and access repository" on each):

| Model | URL |
|-------|-----|
| speaker-diarization-3.1 | https://huggingface.co/pyannote/speaker-diarization-3.1 |
| segmentation-3.0 | https://huggingface.co/pyannote/segmentation-3.0 |
| speaker-diarization-community-1 | https://huggingface.co/pyannote/speaker-diarization-community-1 |
| embedding | https://huggingface.co/pyannote/embedding |
| wespeaker-voxceleb-resnet34-LM | https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM |

> You must accept **all five**. Missing any one will cause a 403 error on first startup.

---

## Step 2 — System dependencies

```bash
sudo apt-get install -y portaudio19-dev python3.13-venv python3.13-dev
```

---

## Step 3 — Configure environment

Copy the example env and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```env
HF_TOKEN=hf_your_token_here
LM_STUDIO_URL=http://192.168.1.76:1234/v1   # your LM Studio address
LM_STUDIO_API_KEY=lm-studio
OBSIDIAN_VAULT=/home/yourname/obsidian-vault/uVault
APP_HOST=0.0.0.0
APP_PORT=8000
```

---

## Step 4 — Install Python dependencies

```bash
# Create venv with Python 3.13
/usr/bin/python3.13 -m venv .venv
source .venv/bin/activate

# PyTorch with CUDA 13.0 (RTX 5090 / Blackwell requires nightly)
pip install --pre "torch>=2.7" "torchaudio>=2.7" \
    --index-url https://download.pytorch.org/whl/nightly/cu130

# If your GPU is older (RTX 3000/4000 series), use the stable build instead:
# pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# All other dependencies
pip install -r requirements.txt
```

---

## Step 5 — Build the frontend

```bash
cd frontend
npm install
npm run build
cd ..
```

---

## Step 6 — Create Obsidian vault directories

```bash
mkdir -p ~/obsidian-vault/AuraScribe/Meetings
mkdir -p ~/obsidian-vault/AuraScribe/People
mkdir -p ~/obsidian-vault/AuraScribe/Daily
```

---

## Running AuraScribe

```bash
source .venv/bin/activate

python tray.py   # recommended — system tray icon, opens browser automatically
# OR
python main.py   # terminal only, then open http://localhost:8000
```

**On first startup**, the app will download model weights in the background (~4GB total, one time only):
- Whisper large-v3 (~3GB)
- pyannote speaker diarization models (~1GB)

The status indicator in the top bar shows **"Loading models"** → **"Ready"** when complete.

---

## First-time voice enrollment

Before your first meeting, enroll your voice so AuraScribe can label you as "Me":

1. Open http://localhost:8000
2. Click the **people icon** (top right)
3. Enter your name (default: "Me")
4. Click **Start Recording** and speak naturally for 10 seconds
5. Done — AuraScribe will now identify your voice automatically

Everyone else will be labeled Person1, Person2, etc. You can click any speaker label in the transcript to rename them (e.g. "Person1" → "John").

---

## Obsidian vault structure

All notes are written to your vault under `AuraScribe/`:

```
obsidian-vault/
└── AuraScribe/
    ├── Meetings/
    │   └── 2026-03-29 Project Kickoff.md
    ├── People/
    │   └── John Smith.md
    └── Daily/
        └── 2026-03-29.md
```

- **Meetings** — full transcript + AI summary + action items, with wikilinks to people
- **People** — auto-updated notes on each person you interact with
- **Daily** — aggregated brief for each day with open action items and tomorrow's priorities

---

## Using the app

| Feature | How |
|---------|-----|
| Start recording | Type an optional meeting title → click "Start Recording" |
| Stop recording | Click "Stop" — summary generates automatically |
| View transcript | Center panel, live as you speak |
| Rename a speaker | Click their label in the transcript header |
| Meeting summary | Right panel → Meeting tab |
| Draft a follow-up email | Right panel → Meeting tab → Draft Email |
| Draft a meeting invite | Right panel → Meeting tab → Draft Invite |
| Daily brief | Right panel → Daily Brief → Generate |

---

## Microphone sharing

AuraScribe does **not** require exclusive mic access. On PipeWire/PulseAudio (Linux), the same microphone works simultaneously in Zoom, Teams, Google Meet, and AuraScribe. Use the **default** device in the recording bar (not the raw `hw:x,x` ALSA device) to ensure sharing works.

---

## LM Studio

AuraScribe sends prompts to whatever model is currently loaded in LM Studio. For best results with meeting summarization, use a model with at least 8B parameters and a long context window (32k+). Recommended: `llama-3.1-8b-instruct` or `mistral-nemo-instruct`.

---

## Troubleshooting

**"No module named 'uvicorn'"** — venv not activated. Run `source .venv/bin/activate` first.

**"PortAudio library not found"** — run `sudo apt-get install -y portaudio19-dev`.

**403 GatedRepoError** — you missed accepting one of the five HuggingFace model licenses. See Step 1c above.

**"NVIDIA GeForce RTX 5090 is not compatible"** — you installed the stable PyTorch instead of nightly. Re-run the `pip install --pre torch` command from Step 4.

**Status stuck on "Loading models"** — models are still downloading (~4GB for Cohere Transcribe + ~1GB for pyannote). Check terminal output for download progress bars.

**No audio devices listed** — PipeWire/PulseAudio may not be running. Run `systemctl --user start pipewire pipewire-pulse`.
