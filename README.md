# AuraScribe

Windows-native, always-on meeting transcription with speaker identification, AI summaries, and Obsidian integration. Runs entirely locally on your GPU.

Built as a Tauri 2 desktop app with a Python sidecar. ASR via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, bundled CUDA runtime). Speaker diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio) 3.1. Summaries via your local LM Studio.

---

## Status

**Phase 1 — scaffold.** Boots Tauri + React + Python sidecar stub. ASR, diarization, LLM, and Obsidian wiring land in subsequent phases.

---

## Requirements

- Windows 11 with WebView2 runtime (preinstalled)
- NVIDIA GPU with recent driver (tested on RTX 5090, 32GB)
- [Rust](https://rustup.rs) + MSVC Build Tools
- [Node.js](https://nodejs.org) 20+
- [Python](https://www.python.org/downloads/) 3.13
- LM Studio running locally with a model loaded (any OpenAI-compatible endpoint works)
- A free HuggingFace account (for pyannote — added in Phase 4)

---

## Setup

```powershell
# 1. Install Tauri CLI (once)
cargo install tauri-cli --version "^2.0"

# 2. Frontend + Rust deps
npm install

# 3. Python sidecar venv
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\sidecar

# 4. Config
copy .env.example .env
# edit .env

# 5. Run
npm run tauri:dev
```

---

## Architecture

```
Tauri shell (Rust + WebView2)
    │
    ├── renders → React UI (Vite)
    │                 │
    │                 └── HTTP + WebSocket → Python sidecar (127.0.0.1:8765)
    │                                              │
    │                                              ├── faster-whisper  (ASR)
    │                                              ├── pyannote.audio  (diarization)
    │                                              ├── LM Studio       (summaries)
    │                                              ├── SQLite          (meetings, people, embeddings)
    │                                              └── Obsidian vault  (markdown notes)
    │
    └── spawns/kills Python sidecar subprocess
```

---

## Project layout

```
aurascribe/
├── src-tauri/          Rust shell (Tauri 2)
├── src/                React 19 + Vite + Tailwind
├── sidecar/            Python 3.13 backend (FastAPI + WS)
├── package.json        Node (frontend + Tauri CLI)
├── vite.config.ts      Dev proxy to sidecar
├── tauri.conf.json     (inside src-tauri/) window + bundle config
├── .env.example        Copy to .env
└── backend/            LEGACY — reference only, deleted end of Phase 2
```

---

## Roadmap

- **Phase 1 (done)** — Tauri + React + sidecar boot.
- **Phase 2** — Port backend modules: DB, config, LLM client, Obsidian writer, audio capture, enrollment.
- **Phase 3** — faster-whisper wiring; end-to-end live transcription.
- **Phase 4** — Real pyannote 3.1 diarization (replaces the single-embedding-per-chunk approach of the Linux version).
- **Phase 5** — Icons, MSI installer, first-run model download UI.
- **Backlog** — NVIDIA Parakeet-TDT 0.6B v3 as an optional ASR backend (currently #1 on Open ASR Leaderboard at 10.72 WER / 1003× RTFx). Needs NeMo, which is painful on Windows — deferred.
- **Backlog** — WASAPI loopback capture for system audio (record Zoom/Teams directly).
