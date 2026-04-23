# AuraScribe

Always-on, fully-local meeting transcription for Windows and macOS. Hands-free start/stop on sustained speech, live AI coaching while you talk, customer-organized notes in Obsidian. Audio, models, and summaries never leave your machine.

Tauri 2 desktop app + Python sidecar. ASR via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio) 3.1. LLM via any OpenAI-compatible endpoint — [LM Studio](https://lmstudio.ai) by default, or OpenAI / Gemini / OpenRouter / Anthropic-compat for frontier models. The sidecar probes hardware at startup and picks Whisper defaults that fit; CPU-only machines work too.

---

## Install

### As a user

Releases: https://github.com/UbhiTS/aurascribe/releases

| Installer | Pick if | Installer | On disk |
|---|---|---|---|
| **AuraScribe-CUDA-setup.exe** | NVIDIA GPU. Streams a ~6 GB CUDA bundle on first launch (~10–20 min on broadband). | ~140 MB | ~6 GB |
| **AuraScribe-CPU-setup.exe** | No GPU, or smaller install. ~5–10× slower transcription, still usable. | ~260 MB | ~700–900 MB |

Per-user install at `%LOCALAPPDATA%\Programs\AuraScribe` (no admin). Data lives at `%APPDATA%\AuraScribe` and survives uninstall.

The CUDA bundle is split + streamed (not shipped in the installer) because the full CUDA payload is ~6 GB — NSIS can't mmap that on the CI runner, and GitHub release assets cap at 2 GB. CI bin-packs heavy files into `<version>.partN.zip` files; the sidecar reads `<version>.manifest.txt` on first launch and reassembles the original layout.

### As a developer

```powershell
cargo install tauri-cli --version "^2.0"
npm install
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".\sidecar[all]"

# NVIDIA GPU? Swap in the CUDA torch wheel (pyannote needs it; ctranslate2 has its own CUDA path):
pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch torchaudio

npm run tauri:dev
```

First launch shows a welcome dialog with your detected hardware and the chosen Whisper defaults. Open **Settings** to configure the LLM endpoint, HuggingFace token, Obsidian vault, or override anything.

### Requirements

- **OS** — Windows 11 (WebView2 preinstalled) or macOS 12+ (arm64).
- **GPU** — *optional*. Any NVIDIA RTX is ideal; GTX 10-series works on `int8`; no GPU → CPU.
- **HuggingFace token** — free, required for pyannote. Accept the gate on `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`, and `pyannote/wespeaker-voxceleb-resnet34-LM`.
- **OpenAI-compat LLM endpoint** — LM Studio at `http://127.0.0.1:1234/v1` is the default. Long-context models (100k+) make Daily Briefs richer.
- From source: Rust + MSVC Build Tools, Node 20+, Python 3.13.

---

## Features

**Adaptive hardware** — sidecar probes `ctranslate2` + `torch.cuda` at boot; picks `large-v3-turbo + cuda + float16` on ≥8 GB VRAM, scales down to `small + cpu + int8` if no GPU. Header chips show where each pipeline runs (Whisper, Diarize). Override any default in Settings.

**Hands-free auto-capture** — opens a lightweight VAD stream when idle; auto-starts a meeting on sustained speech, auto-stops after sustained silence. Once silence has lasted past a configurable gate (default 5s), the Stop button morphs into a live `Stop in MM:SS` countdown so you know exactly how long until auto-stop fires — click to stop now, or let it run out. Manual recordings ignore auto-stop.

**Three audio sources** — **Microphone**, **System Audio** (WASAPI loopback / BlackHole captures Zoom/Teams/Meet participants), or **Mix** with Speex-AEC cancelling the loopback echo out of the mic so speakers-in-room don't double up. Last-used source persists by device name. Pre-recording VU + waveform animate the *selected* source so you can verify before hitting Start. Mic-permission failures surface a one-click "Open mic settings" modal.

**Live transcription** — VAD-gated chunks through faster-whisper. A 1.5s speculative loop transcribes the audio tail so partial lines appear while you speak. UI re-adopts in-flight recordings across HMR / dev restarts. Live-tail scroll pinning: scrolled up reading? viewport stays put.

**Speakers / Voices** — pyannote 3.1 emits a 256-dim embedding per utterance. Tag-as-you-go (no upfront enrollment); the embedding folds into the Voice's pool, future chunks match via cosine distance. Unknowns are clustered live into `Speaker 1/2/…` rather than dumped into one bucket. Per-utterance re-assignment is fully lossless. Recompute re-runs diarization over a saved meeting against the current Voices DB. The Voices page lists every speaker with samples, snippets, merge / delete / rename / recolour.

**Live AI coaching** — debounced LLM call (~20s after last utterance, hard-cap 60s) extracts highlights + action items (yours and others'). A 2–5 bullet support-intelligence card suggests what to say next: tools to mention, counterarguments to pre-empt, clarifying questions. Live title refinement: same call returns an entity + topic so the meeting title sharpens as it progresses (freeze with the lock icon to stop overrides). A progress bar shows when the next refresh fires.

**Customer-organized vault** — Obsidian writer routes each finished meeting into a bucket under `<vault>/`:

```
00-Inbox/YYYY/MM/                     unclassified / low-confidence
10-Customers/<Customer>/
    <Customer>.md                     MOC, seeded on first meeting
    Meetings/YYYY/MM/<filename>.md
    People/<Name>.md
    Notes/{Architecture,Stakeholders,Open-Risks,Commercials,Notes}.md
20-Internal/   (Meetings/, People/, Notes/)
30-Interviews/YYYY/MM/
40-Personal/YYYY/MM/
50-Daily/YYYY/MM/YYYY-MM-DD.md
90-Templates/  (seeded on boot)
99-Archive/    (manual)
```

Bucket + customer are inferred at finalize: cheap speaker-folder lookup first (every named speaker → who they belong to → majority wins), LLM fallback gated at 0.5 confidence (`meeting_bucket.md`). Low-confidence routes land in Inbox for triage. The customer name in `vault_customer` triggers a one-time bootstrap of the MOC + canonical Notes/ files.

**Daily Brief** — end-of-day rollup of every meeting on a date: tl;dr, highlights, decisions, open threads, action items, per-person takeaways, themes, tomorrow's focus. Marked stale and rebuilt in the background when a meeting finishes (toggleable).

**Meeting editing** — rename (Obsidian file moves to match), trim before/after a timestamp, split at a timestamp (both halves get their own files), bulk-delete by id list or "last N days", click-to-play any utterance into the Opus recording at that exact offset, summarize + suggest title in one LLM pass.

**Editable prompts** — `live_intelligence.md`, `daily_brief.md`, `meeting_analysis.md`, `meeting_bucket.md` live under `APP_DATA/prompts/` (seeded from bundled defaults). One-click open from Settings; edits picked up on the next call.

**Diagnostics** — React error boundary catches blank-screen renders; sidecar `sys.excepthook` writes `crash-YYYYMMDD-HHMMSS.log`; Rust panic hook writes `crash-<unix>-rust.log`; rotating file log (5 × 5 MB). Heartbeat polls `/api/status` every 30s — header flips to `Sidecar unreachable` if the process dies.

---

## Configuration

Everything sits under one **data directory** (default `%APPDATA%\AuraScribe`). The folder holds the SQLite DB, per-meeting `.opus` files, Whisper model cache, logs, editable prompts, and `config.json`. Copy it to migrate.

| Key | Default | Purpose |
|---|---|---|
| `hf_token` | — | HuggingFace token for pyannote |
| `llm_base_url` | `http://127.0.0.1:1234/v1` | OpenAI-compat root |
| `llm_api_key` | `lm-studio` | Provider key (any non-empty for LM Studio) |
| `llm_model` | `local-model` | Model id (`gpt-4o`, `gemini-2.0-flash`, etc.) |
| `llm_context_tokens` | `4096` | Total context budget |
| `whisper_model` / `whisper_device` / `whisper_compute_type` | auto | Override the hardware-aware defaults |
| `whisper_language` | `en` | ISO code; empty for auto-detect |
| `my_speaker_label` | `Me` | How your voice is labelled |
| `obsidian_vault` | — | Vault root; empty disables Obsidian writes |
| `rt_highlights_debounce_sec` / `_max_interval_sec` / `_window_sec` | `20` / `60` / `180` | Live-intel cadence + transcript window |
| `auto_capture_enabled` | `true` | Master switch for hands-free start/stop |
| `auto_capture_start_speech_sec` | `1.5` | Sustained speech before auto-start |
| `auto_capture_stop_silence_sec` | `30` | Sustained silence before auto-stop |
| `auto_capture_countdown_after_silence_sec` | `5` | Silence before the Stop button shows the countdown |
| `auto_capture_vad_threshold` | `0.5` | Listening sensitivity (raise in noisy rooms) |

**Advanced** (Advanced Settings block — defaults match the old hard-coded values):

| Key | Default | Purpose |
|---|---|---|
| `chunk_duration` / `silence_duration` / `vad_threshold` | `10.0` / `0.6` / `0.5` | Audio chunk + VAD gating |
| `aec_tail_ms` | `200` | Echo-canceller tail in Mix mode |
| `voice_match_threshold_multi` / `_solo` / `voice_ratio_margin` / `min_voice_samples` | `0.55` / `0.70` / `0.80` / `3` | Speaker-match tuning |
| `provisional_threshold` | `0.50` | Live `Speaker 1/2/…` clustering gate |
| `speculative_interval_sec` / `_window_sec` | `1.5` / `30.0` | Live-partial loop |
| `obsidian_write_interval_sec` / `_chunks` | `15.0` / `5` | Throttle live vault writes (whichever fires first) |
| `daily_brief_auto_refresh` | `false` | Auto-regen brief when a meeting finishes |

Settings UI shows the detected hardware (`Detected: cuda · NVIDIA RTX 4090 · 24 GB VRAM`) and tags each value `custom` / `default`. Most values need a sidecar restart; auto-capture knobs hot-reload. Sidecar bind: `SIDECAR_HOST` / `SIDECAR_PORT` env (default `127.0.0.1:8765`).

**DB policy** — pre-GA. Schema changes drop and recreate (no migrations). `db/database.py` bumps a `_CURRENT_SCHEMA_VERSION` string; mismatch → fresh DB on next boot.

---

## Architecture

```
Tauri shell (Rust + WebView2 / WKWebView)
    │
    ├── React 19 UI (Vite + Tailwind + lucide-react)
    │       │
    │       └── HTTP + WebSocket → Python sidecar (127.0.0.1:8765)
    │                                    │
    │                                    ├── faster-whisper      (ASR; CUDA or CPU)
    │                                    ├── pyannote.audio 3.1  (diarization + 256-dim embeddings)
    │                                    ├── Silero VAD          (auto-capture + chunking)
    │                                    ├── sounddevice / soxr  (mic capture, Speex-AEC)
    │                                    ├── soundfile           (Opus recorder)
    │                                    ├── LLM client          (OpenAI-compat)
    │                                    ├── SQLite (aiosqlite)  (meetings, voices, embeddings, vault routing)
    │                                    └── Obsidian writer     (bucket-routed markdown)
    │
    ├── spawns/kills sidecar subprocess
    └── panics → APPDATA\AuraScribe\logs\crash-*-rust.log
```

Sidecar binds to `127.0.0.1` only; CORS is locked to the Vite dev origin + Tauri webview schemes. On Windows, `nvidia-*-cu12` `bin/` directories are registered via `os.add_dll_directory` at import so CTranslate2 finds CUDA 12 runtimes without touching system PATH.

---

## Project layout

```
aurascribe/
├── .github/workflows/release.yml         CI: matrix CUDA/CPU builds → draft release
├── src-tauri/                            Rust shell (Tauri 2)
├── src/                                  React 19 + Vite + Tailwind
│   ├── App.tsx                           Root state, heartbeat poll, WS dispatch, page router
│   ├── components/                       Shell, Header, RecordingBar, TranscriptView,
│   │                                     VuMeter, Waveform, AutoCaptureChip, …
│   ├── pages/                            LiveFeed, MeetingLibrary, Review, Voices,
│   │                                     DailyBrief, Settings
│   └── lib/                              api.ts, useWebSocket, useLLMHealth, MicAudioContext
├── sidecar/                              Python 3.13 backend
│   ├── main.py                           uvicorn entry + crash excepthook
│   ├── build.ps1                         PyInstaller driver (auto-aligns torch wheel to host GPU)
│   └── aurascribe/
│       ├── api.py                        FastAPI app, CORS, lifespan, /ws, /api/status
│       ├── config.py                     Hardware probe, data-dir + config.json loader
│       ├── auto_capture.py               VAD-driven start/stop monitor + state machine
│       ├── meeting_manager.py            Recording lifecycle, speculative loop, finalize
│       ├── routes/                       meetings, voices, settings, daily_brief, intel,
│       │                                 plus _shared (singletons + helpers)
│       ├── audio/                        sounddevice capture, Silero VAD, Opus recorder
│       ├── transcription/                faster-whisper + pyannote, voice-pool matching
│       ├── llm/                          client, prompts, analysis, realtime (live intel
│       │                                 + title refinement), daily_brief, bucket_inference,
│       │                                 plus user-editable .md prompts
│       ├── obsidian/writer.py            Bucket-routed markdown writer + customer bootstrap
│       └── db/database.py                SQLite schema + drop-and-recreate version gate
├── package.json                          Scripts: dev, tauri:dev, tauri:build, build:sidecar, package
└── vite.config.ts                        Dev proxy + manual vendor chunks
```

---

## REST / WebSocket surface

Authoritative list lives in [sidecar/aurascribe/routes/](sidecar/aurascribe/routes/). Headlines:

**Meetings** (`/api/meetings/*`) — `POST /start`, `POST /stop`, `GET /` (paged), `GET/DELETE/PATCH /{id}`, `/{id}/transcript`, `/{id}/audio` (Opus, Range), `/summarize`, `/suggest-title`, `/title-lock`, `/trim`, `/split`, `/rename-speaker`, `/utterances/{uid}/assign`, `/intel/refresh`, `/recompute`, `POST /bulk-delete`, `DELETE /all?days=`.

**Voices** (`/api/voices/*`) — list, detail, rename / recolour / delete, snippet-delete, `POST /merge`.

**Settings** (`/api/settings/*`) — `GET/PUT /data-dir`, `GET/PUT /config` (response carries `requires_restart`).

**Daily Brief** (`/api/daily-brief/*`) — `GET /?date=`, `POST /refresh?date=`.

**Intel** (`/api/intel/*`) — `GET /prompts`, `POST /open-prompt`, `GET /prompt-path`.

**System** — `GET /api/status`, `GET /api/models`, `GET/PUT /api/auto-capture`, `POST /api/system/open-mic-settings`.

**WebSocket** `/ws` — push channel: `utterances`, `partial_utterance`, `status`, `audio_level`, `realtime_intelligence`, `title_updated`, `auto_capture`, `daily_brief_updated`.

---

## Building installers

```powershell
npm run build:sidecar   # PyInstaller onedir bundle (10-15 min, ~1.5 GB)
npm run tauri:build     # Tauri build + NSIS installer
npm run package         # both, in sequence
```

`build:sidecar` auto-aligns your venv's torch wheel to the host GPU before PyInstaller runs (`nvidia-smi` detects GPU; mismatches trigger a `pip install --index-url cu128` or `cpu`). CI pins the wheel upfront and skips this step.

**CI** — push a tag (`git tag v0.2.0 && git push origin v0.2.0`). [.github/workflows/release.yml](.github/workflows/release.yml) builds CUDA + CPU variants in parallel, splits the CUDA bundle into release-asset-sized parts, drafts a GitHub Release with both installers + the runtime zip + a picker table. You review and click Publish. `workflow_dispatch` runs builds without releasing.

---

## Logs

Sidecar logs (incl. uvicorn) → stdout *and* `%APPDATA%\AuraScribe\logs\sidecar.log` (5 × 5 MB rotating). Crashes drop a sibling `crash-YYYYMMDD-HHMMSS.log` (Python) or `crash-<unix>-rust.log` (Rust). Useful grep prefixes: `provisional:`, `voice-match:`, `diarize:`, `asr:`, `daily_brief:`, `auto-capture:`, `extras:`. Frontend console: right-click → Inspect (devtools enabled in debug).

---

## Roadmap

- **Done** — Tauri + React + sidecar; SQLite + Obsidian writer; live transcription + speculative partials; pyannote 3.1 diarization; tag-as-you-go Voices + provisional clustering + per-utterance re-assignment; recompute; live intel (highlights + action items + support coaching) with same-call title refinement + freeze; daily brief with auto-refresh; meeting trim / split; mid-recording re-adoption; NSIS installer + PyInstaller bundling; CI matrix for CUDA/CPU; hardware auto-detect + user-overridable; first-run welcome dialog; file-based logs + crash dumps; mic-permission detection; error boundaries; follow-tail scroll pinning; persistent globally-unique speaker colors + custom avatar upload; WASAPI loopback + Speex-AEC mix; macOS arm64 support; **auto-capture (sustained-speech start, sustained-silence stop) with countdown-on-Stop-button**; **customer-isolated vault layout with bucket inference at finalize + customer MOC bootstrap**.
- **Next** — Code-signing for the installer (kill the SmartScreen "Unknown publisher" warning).
- **Backlog** — NVIDIA Parakeet-TDT 0.6B v3 as an optional ASR backend (NeMo on Windows is painful; deferred). Multi-vault routing. Scheduled meeting pre-briefs (calendar + prior-meeting context). Opt-in telemetry / crash uploader. MCP server (expose meetings / voices / intel to external agents). In-Settings mic test panel.

---

## Development notes

- **Sidecar-only** — `.venv\Scripts\python sidecar\main.py`; check `http://127.0.0.1:8765/api/status`.
- **Frontend-only** — `npm run dev` (Vite on 1420, proxies `/api` + `/ws` to 8765).
- **Routes** — `api.py` is just FastAPI wiring. Add a feature area: new `routes/<name>.py` with `router = APIRouter(...)`, import in `routes/__init__.py`. Cross-router state lives in `routes/_shared.py`.
- **Adding a config key** — four edits: module-level getter in `config.py`, `_CONFIG_KEYS` in same file, `_CONFIG_FIELDS` + `_effective_for` + `UserConfigUpdate` in `routes/settings.py`, `ConfigKey` union + Settings UI field in the frontend.
- **CUDA torch in dev** — your venv defaults to the CPU torch wheel from PyPI. Run `npm run build:sidecar` once to auto-swap, or do it manually with the `--index-url cu128` line above.
- **Rust panics in dev** — show as an error dialog + a crash file under `%APPDATA%\AuraScribe\logs\`. Release builds exit cleanly after the dialog.
