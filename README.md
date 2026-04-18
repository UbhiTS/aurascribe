# AuraScribe

Windows-native, always-on meeting transcription with speaker identification, live AI coaching, and Obsidian integration. Runs entirely locally on your GPU — audio, models, and summaries never leave the machine.

Built as a Tauri 2 desktop app with a Python sidecar. ASR via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, bundled CUDA runtime). Speaker diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio) 3.1. LLM calls (summaries, real-time highlights, daily briefs) go to a local [LM Studio](https://lmstudio.ai) OpenAI-compatible endpoint.

---

## Features

### Transcription
- **Live transcription** — ~10s rolling chunks, GPU-accelerated faster-whisper (`large-v3-turbo` by default). Speech-gated with VAD; silence between utterances is skipped.
- **Speculative partials** — a 1.5s speculative loop transcribes the tail of the audio buffer so a partial line appears while you speak, updated until the next chunk finalizes.
- **Session re-adoption** — if the UI reloads mid-recording (HMR, window refresh, dev restart), the frontend re-attaches to the sidecar's in-flight meeting instead of dropping the stream.

### Speakers
- **Diarization** — pyannote 3.1 pipeline segments each chunk into turns and emits a speaker embedding per utterance.
- **Speaker enrollment** — record a ~10s sample; the embedding is stored and future chunks are matched against the enrolled pool.
- **Provisional clustering** — unknown speakers are clustered in-memory per meeting (centroid-based cosine distance) so you see `Speaker 1` / `Speaker 2` instead of a single "Unknown" bucket.
- **Rename as you go** — renaming a provisional label to a real name folds every matched embedding into that person's enrollment pool. Effectively "tag-as-you-go" enrollment.
- **Per-utterance re-assignment** — click a line, pick or create a speaker; the embedding is folded in and the matcher improves online. Previous mis-tags are fully undoable.

### Real-time intelligence
- **Live highlights panel** — debounced LLM call (~20s after the last utterance, hard-capped at 60s) extracts new highlights, action items for you, and action items for other speakers.
- **Support intelligence** — a 2–5 bullet side-panel card suggesting what to say next: specific tools/numbers to mention, counterarguments to pre-empt, clarifying questions to ask. Refreshed on every tick.
- **Persistence** — highlights and action items accumulate across the meeting and are written into the Obsidian note as they land.
- **Editable prompts** — `realtime_highlights.md` and `daily_brief.md` live next to the code and are picked up on the next run. One-click "open in editor" from Settings.

### Daily Brief
- **End-of-day rollup** — aggregates every meeting on a given date into a single briefing: tl;dr, highlights, decisions, open threads, action items (yours + others'), per-person takeaways, themes, tomorrow's focus, coaching notes.
- **Auto-refresh** — marked stale and rebuilt in the background whenever a meeting on that date finishes.
- **Long-context aware** — input budget is tuned to the configured LM Studio context window (default 220k tokens) so a full day of transcripts fits in one call.

### Meeting editing
- **Rename** — mid-recording or post-hoc; the Obsidian file is moved to the new filename.
- **Trim** — drop everything before or after a timestamp. Remaining utterances rebase to 0; `started_at` shifts accordingly.
- **Split** — cut a meeting in two at a timestamp. Both halves get their own Obsidian file and cleared summaries.
- **Bulk delete** — clear by ID list or "everything in the last N days".
- **Summarize on demand** — re-run the summary pass (and per-person notes) without re-recording.

### Storage & integrations
- **SQLite** — meetings, utterances (with embeddings), people, speaker enrollments. Lives in `%APPDATA%\AuraScribe\aurascribe.db`.
- **Obsidian vault** — meeting notes, people notes, and daily briefs written under `<vault>/AuraScribe/{Meetings,People,Daily}`, organized `YYYY/MM/*.md`. Live-updated during recording on a throttled write (15s or every 5 chunks, whichever trips first) so Obsidian sync watchers don't thrash.
- **WebSocket feed** — single `/ws` channel broadcasts utterances, partials, status events, real-time intelligence, and daily-brief state changes to any connected client.

---

## Requirements

- **OS** — Windows 11 with WebView2 runtime (preinstalled on current builds)
- **GPU** — NVIDIA with a recent driver. Developed and tested on RTX 5090 (32 GB). Smaller GPUs will work with a smaller Whisper model (`distil-large-v3` or `medium`).
- **Rust** — [rustup](https://rustup.rs) + MSVC Build Tools (for `tauri build`)
- **Node.js** — 20+
- **Python** — 3.13 (sidecar targets `>=3.13`)
- **LM Studio** — running locally with an OpenAI-compatible endpoint at `http://127.0.0.1:1234/v1`. Any model loaded works; long-context models (100k+) unlock the Daily Brief on busy days.
- **HuggingFace account** — free token is required to download the pyannote 3.1 pipeline. Accept the model licenses on the HF pages first.

---

## Setup

```powershell
# 1. Tauri CLI (one-time, global)
cargo install tauri-cli --version "^2.0"

# 2. Frontend + Rust deps
npm install

# 3. Python sidecar — 3.13 venv at repo root
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".\sidecar[all]"

# 4. Config
copy .env.example .env
# edit .env — set HF_TOKEN, OBSIDIAN_VAULT, LM_STUDIO_MODEL

# 5. Run
npm run tauri:dev
```

The Tauri shell spawns the Python sidecar automatically (see [src-tauri/src/lib.rs](src-tauri/src/lib.rs)). On first run the sidecar downloads the Whisper model (`~/.cache/huggingface` or `%APPDATA%\AuraScribe\models`) and, once you accept the licenses, the pyannote pipeline.

### HuggingFace licenses (one-time)
Accept the license on each of these HF pages with the same account as your token:
- `pyannote/speaker-diarization-3.1`
- `pyannote/segmentation-3.0`
- `pyannote/embedding` (and any dependent models the pipeline pulls)

If enrollment or diarization fails with a 401 / "GatedRepo" error, that's what's missing.

### Optional installs

The sidecar's extras are split so you can install only what you need:

```powershell
pip install -e ".\sidecar"                # core FastAPI server (no ASR)
pip install -e ".\sidecar[asr]"           # + faster-whisper, sounddevice
pip install -e ".\sidecar[diarization]"   # + torch, torchaudio, pyannote
pip install -e ".\sidecar[llm]"           # + openai SDK for LM Studio
pip install -e ".\sidecar[all]"           # everything (recommended)
pip install -e ".\sidecar[dev]"           # pytest, ruff
```

---

## Configuration

All config goes through `.env` at the repo root. See [.env.example](.env.example) for the authoritative list. Highlights:

| Key | Default | Purpose |
|---|---|---|
| `HF_TOKEN` | — | HuggingFace token for pyannote downloads |
| `LM_STUDIO_URL` | `http://127.0.0.1:1234/v1` | OpenAI-compatible endpoint |
| `LM_STUDIO_MODEL` | `local-model` | Model ID LM Studio should load |
| `LM_STUDIO_CONTEXT_TOKENS` | `220000` | Context budget for Daily Brief |
| `OBSIDIAN_VAULT` | — | Vault root; omit to disable Obsidian writes |
| `WHISPER_MODEL` | `large-v3-turbo` | faster-whisper model id |
| `WHISPER_DEVICE` | `cuda` | `cuda` / `cpu` |
| `WHISPER_COMPUTE_TYPE` | `float16` | `float16` / `int8_float16` / `int8` |
| `WHISPER_LANGUAGE` | `en` | ISO code or empty for auto-detect |
| `DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` | Pipeline id |
| `MY_SPEAKER_LABEL` | `Me` | How your enrolled voice is labeled |
| `SIDECAR_HOST` / `SIDECAR_PORT` | `127.0.0.1` / `8765` | Sidecar bind address |
| `SPEAKER_PROVISIONAL_THRESH` | `0.50` | Cosine-distance threshold for clustering unknowns |
| `RT_HIGHLIGHTS_DEBOUNCE_SEC` | `20` | Seconds after last utterance before an intel call fires |
| `RT_HIGHLIGHTS_MAX_INTERVAL_SEC` | `60` | Hard cap between intel calls during continuous speech |
| `RT_HIGHLIGHTS_WINDOW_SEC` | `180` | Transcript window the intel LLM sees |
| `VAULT_WRITE_INTERVAL_SEC` | `15` | Throttle for live Obsidian writes |
| `VAULT_WRITE_CHUNKS` | `5` | Chunks between forced live Obsidian writes |
| `AURASCRIBE_DATA` | `%APPDATA%\AuraScribe` | Durable state (DB, models) |

---

## Architecture

```
Tauri shell (Rust + WebView2)
    │
    ├── renders → React 19 UI (Vite + Tailwind + lucide-react)
    │                 │
    │                 └── HTTP + WebSocket → Python sidecar (127.0.0.1:8765)
    │                                              │
    │                                              ├── faster-whisper       (ASR, CTranslate2 + CUDA)
    │                                              ├── pyannote.audio 3.1   (diarization + embeddings)
    │                                              ├── sounddevice / scipy  (mic capture, VAD, chunking)
    │                                              ├── LM Studio client     (summaries, real-time intel, daily brief)
    │                                              ├── SQLite (aiosqlite)   (meetings, people, utterances, embeddings)
    │                                              └── Obsidian writer      (Meetings/People/Daily markdown)
    │
    └── spawns/kills Python sidecar subprocess
```

The sidecar binds to `127.0.0.1` only — the Tauri shell is the only process that can reach it. On Windows, the nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip packages' `bin/` directories are registered via `os.add_dll_directory` at import time so CTranslate2 finds CUDA 12 runtimes without touching the system PATH.

---

## Project layout

```
aurascribe/
├── src-tauri/                      Rust shell (Tauri 2)
│   ├── src/lib.rs                  Sidecar spawn/kill
│   └── tauri.conf.json             Window + bundle config
├── src/                            React 19 + Vite + Tailwind
│   ├── components/                 Shell, Sidebar, TranscriptView, RecordingBar, VuMeter, ...
│   ├── pages/                      LiveFeed, MeetingLibrary, Review, Enrollment, DailyBrief, Settings
│   └── lib/                        api.ts, useWebSocket, MicAudioContext
├── sidecar/                        Python 3.13 backend
│   ├── main.py                     uvicorn entry point
│   └── aurascribe/
│       ├── api.py                  FastAPI routes + WS
│       ├── config.py               .env loader, paths, thresholds
│       ├── meeting_manager.py      Lifecycle, recording loop, provisional clustering
│       ├── audio/                  capture.py, enrollment.py
│       ├── transcription/          engine.py, whisper.py (faster-whisper + pyannote wiring)
│       ├── llm/                    client.py, prompts.py, realtime.py, daily_brief.py (+ .md prompts)
│       ├── obsidian/writer.py      Meeting / people / daily-brief markdown writers
│       └── db/database.py          SQLite schema + migrations
├── package.json                    Node (frontend + Tauri CLI)
├── vite.config.ts                  Dev proxy to sidecar
└── .env.example                    Copy to .env
```

---

## REST / WebSocket surface

The sidecar exposes a small JSON API plus a single WebSocket for push events. Full reference lives in [sidecar/aurascribe/api.py](sidecar/aurascribe/api.py); the headlines:

- `POST /api/meetings/start` `{ title, device }` → begin recording
- `POST /api/meetings/stop` `{ summarize }` → stop, finalize, optionally summarize
- `GET /api/meetings?limit=&offset=&days=` — recent meetings
- `GET /api/meetings/{id}` / `DELETE` / `PATCH` — read / remove / rename
- `POST /api/meetings/{id}/summarize` — (re)run the summary + people notes
- `POST /api/meetings/{id}/trim` `{ before, after }` — crop transcript
- `POST /api/meetings/{id}/split` `{ at, new_title }` — split at timestamp
- `POST /api/meetings/{id}/rename-speaker` — bulk rename + enrollment fold-in
- `POST /api/meetings/{id}/utterances/{uid}/assign` — tag one line
- `POST /api/meetings/{id}/intel/refresh` — force a real-time intel pass
- `GET /api/daily-brief?date=YYYY-MM-DD` / `POST /api/daily-brief/refresh`
- `GET /api/intel/prompts` / `POST /api/intel/open-prompt` — prompt editing
- `GET /api/people` / `POST /api/enroll/start` — enrollment
- `GET /api/status`, `GET /api/models`
- `WS /ws` — `utterances`, `partial_utterance`, `status`, `realtime_intelligence`, `daily_brief_updated`

---

## Roadmap

- **Done** — Tauri + React + sidecar scaffold; SQLite + config + LLM client + Obsidian writer; live transcription with speculative partials; pyannote 3.1 diarization; enrollment + provisional clustering + tag-as-you-go rename; per-utterance re-assignment; real-time intelligence (highlights, action items, support coaching); editable prompt files; daily brief aggregation; YYYY/MM vault structure; meeting trim + split; mid-recording re-adoption after UI reload.
- **Next** — Icons, MSI installer, first-run model download UI (no manual venv step for end users).
- **Next** — WASAPI loopback capture for system audio (record Zoom / Teams / Google Meet directly, without a physical loopback device).
- **Backlog** — NVIDIA Parakeet-TDT 0.6B v3 as an optional ASR backend (currently #1 on Open ASR Leaderboard at 10.72 WER / 1003× RTFx). Needs NeMo, which is painful on Windows — deferred.
- **Backlog** — Multi-vault / per-meeting vault routing.
- **Backlog** — Scheduled meeting pre-briefs (pull upcoming calendar entries + relevant prior meetings into a pre-meeting card).

---

## Development notes

- **Sidecar-only work** — `python sidecar/main.py` brings up the FastAPI server without Tauri. Hit `http://127.0.0.1:8765/api/status` to verify.
- **Frontend-only work** — `npm run dev` runs Vite standalone on port 1420; the WS client will reconnect automatically once a sidecar appears on 8765.
- **Building an installer** — `npm run tauri:build` produces an MSI under `src-tauri/target/release/bundle/msi`. The Python venv is not yet bundled; that lands with the installer UI on the roadmap.
- **Logs** — the sidecar logs at INFO to the Tauri dev console. Look for `provisional:`, `intel:`, and `daily_brief:` prefixes when debugging.
