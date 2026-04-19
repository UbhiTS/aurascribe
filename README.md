# AuraScribe

Windows-native, always-on meeting transcription with speaker identification, live AI coaching, and Obsidian integration. Everything runs locally — audio, models, and summaries never leave the machine.

Built as a Tauri 2 desktop app with a Python sidecar. ASR via [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 with a CUDA or CPU backend). Speaker diarization via [pyannote.audio](https://github.com/pyannote/pyannote-audio) 3.1. LLM calls go to any OpenAI-compatible endpoint — [LM Studio](https://lmstudio.ai) locally by default, or OpenAI / OpenRouter / Gemini / Anthropic-compat proxies for frontier models.

Runs on any modern Windows PC — GPU recommended, but **it works on CPU too**. The app probes your hardware at startup and picks appropriate Whisper defaults (model size, device, precision).

---

## Install

Two ways in, depending on who you are.

### As a user — grab an installer

Releases: https://github.com/UbhiTS/aurascribe/releases

Two flavors per release, pick one:

| Installer | When to pick it | Size |
|---|---|---|
| **AuraScribe-CUDA-setup.exe** | You have an NVIDIA GPU (any RTX card). Fastest transcription + GPU-accelerated diarization. | ~1.5–2 GB |
| **AuraScribe-CPU-setup.exe** | No GPU, or you want the smaller install. Transcription is ~5–10× slower but still usable. | ~700–900 MB |

Both install per-user to `%LOCALAPPDATA%\Programs\AuraScribe` — no admin rights needed. User data (DB, audio, logs, models) lives under `%APPDATA%\AuraScribe` and survives uninstall.

Not sure? Install CUDA — it works on machines without a GPU too, it just wastes ~1 GB of disk.

### As a developer — from source

```powershell
# 1. Tauri CLI (one-time, global)
cargo install tauri-cli --version "^2.0"

# 2. Frontend + Rust deps
npm install

# 3. Python sidecar — 3.13 venv at repo root
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".\sidecar[all]"

# 4. If you have an NVIDIA GPU, grab CUDA torch (pyannote diarization
#    only goes to GPU with a CUDA-enabled torch wheel — ctranslate2 has
#    its own CUDA path and is independent):
pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch torchaudio

# 5. Run dev
npm run tauri:dev
```

On first launch you'll see a welcome dialog describing what hardware was detected and which defaults were chosen. Open **Settings** to configure the LLM endpoint, HuggingFace token, Obsidian vault, or override any of the ASR defaults. Everything persists in `config.json` inside the data directory.

### Requirements

- **OS** — Windows 11 with WebView2 runtime (preinstalled on current builds).
- **GPU** — *optional*. Any NVIDIA RTX card (20/30/40/50 series) is ideal. GTX 10-series works with `int8` compute. No NVIDIA card → CPU fallback.
- **Rust** — [rustup](https://rustup.rs) + MSVC Build Tools (only if building from source).
- **Node.js** — 20+ (only if building from source).
- **Python** — 3.13 (sidecar targets `>=3.13`; only if building from source).
- **An OpenAI-compatible LLM endpoint** — LM Studio at `http://127.0.0.1:1234/v1` is the default, but anything that speaks `/v1/chat/completions` works. Long-context models (100k+) unlock richer Daily Briefs.
- **HuggingFace token** — free; required to download the pyannote pipeline. Accept the licences on `pyannote/speaker-diarization-3.1`, `pyannote/segmentation-3.0`, and `pyannote/wespeaker-voxceleb-resnet34-LM` with the same account. A 401/"GatedRepo" error at load time means a licence still needs accepting.

---

## Features

### Adaptive hardware use
- **Auto-detect at startup** — the sidecar probes `ctranslate2.get_cuda_device_count()` and `torch.cuda.is_available()` at import and picks defaults that fit: `large-v3-turbo + cuda + float16` on ≥8 GB VRAM, `int8_float16` on 4-8 GB, `medium + int8` on <4 GB, `small + cpu + int8` if no GPU.
- **Header chips** show exactly where each pipeline is running — `Whisper · large-v3-turbo · GPU` and `Diarize · GPU` (emerald) or `CPU` (amber). You always know where the compute is happening.
- **Override anything** — Settings → Speech & Transcription has dropdowns for `whisper_device`, `whisper_compute_type`, and a free-text model field. Auto-detect comes back when you clear the override.

### Transcription
- **Live transcription** — ~10s VAD-gated chunks through faster-whisper. Silence between utterances is skipped.
- **Speculative partials** — a 1.5s speculative loop transcribes the tail of the audio buffer so a partial line appears while you speak, updated until the next chunk finalizes.
- **Session re-adoption** — if the UI reloads mid-recording (HMR, window refresh, dev restart), the frontend re-attaches to the sidecar's in-flight meeting instead of dropping the stream.
- **Live-tail scroll pinning** — auto-scrolls the transcript only when you're parked at the live edge. Scrolled up reading? The viewport stays put. `overflow-anchor` stabilises layout shifts from the growing live partial bubble.

### Speakers (Voices)
- **Diarization** — pyannote 3.1 segments each chunk into turns and emits a 256-dim speaker embedding per utterance.
- **Tag-as-you-go** — no upfront enrollment. Click a pill, pick or create a Voice; the embedding folds into that Voice's pool, and future chunks match via cosine distance against the whole pool.
- **Provisional clustering** — unknown speakers in a live meeting are clustered in-memory into `Speaker 1`, `Speaker 2`... rather than a single "Unknown" bucket. Renaming a provisional label folds every matched embedding into the real Voice.
- **Per-utterance re-assignment** — fix a mistag on any line; the old embedding is removed from its prior pool and moved to the new one, so mistakes are fully lossless.
- **Recompute** — after tagging, re-run diarization over a saved meeting to relabel every utterance against the current Voices DB.
- **Voice library** — Voices page shows every tagged speaker with sample count (≥3 samples = active in auto-match), playable snippets, merge, delete, rename, recolour.

### Real-time intelligence
- **Live highlights panel** — debounced LLM call (~20s after the last utterance, hard-capped at 60s) extracts new highlights, action items for you, and action items for other speakers.
- **Support intelligence** — a 2-5 bullet side-panel card suggesting what to say next: specific tools/numbers to mention, counterarguments to pre-empt, clarifying questions to ask. Refreshed on every tick.
- **Progress bar** — the Live Intelligence panel visualises both timers (20s debounce + 60s max) so you can see when the next refresh will fire.
- **Editable prompts** — `live_intelligence.md` and `daily_brief.md` live under `APP_DATA/prompts/` (seeded from bundled copies on first run). One-click "open in editor" from Settings; edits are picked up on the next LLM call, no restart needed.

### Daily Brief
- **End-of-day rollup** — aggregates every meeting on a given date into a single briefing: tl;dr, highlights, decisions, open threads, action items (yours + others'), per-person takeaways, themes, tomorrow's focus, coaching notes.
- **Auto-refresh** — marked stale and rebuilt in the background whenever a meeting on that date finishes. A `daily_brief_updated` WS event pushes the UI when the refresh lands.
- **Long-context aware** — input budget is tuned to the configured LLM context window; long-context models let a full day of transcripts fit in one call.

### Meeting editing
- **Rename** — mid-recording or post-hoc; Obsidian file is moved to match.
- **Trim** — drop everything before or after a timestamp. Remaining utterances rebase to 0; `started_at` shifts accordingly.
- **Split** — cut a meeting in two at a timestamp. Both halves get their own Obsidian file and cleared summaries.
- **Bulk delete** — clear by ID list or "everything in the last N days".
- **Summarize + suggest title** — one LLM call produces both. Placeholder-titled meetings (`Transcription 2026-04-18 14:22`) auto-rename to the suggested title; user-chosen titles are never overwritten.
- **Click-to-play** — every utterance has a Play icon that seeks into the Opus recording at that exact offset.

### First-run UX
- **Welcome dialog** (one-time, per install) — shown after models load, explains the detected hardware and the three defaults chosen (model, device, precision), with an Open Settings shortcut.
- **Staged load progress** — splash shows `Downloading Whisper model 'large-v3-turbo'... (1-5 min, one-time)` on first run, `Loading...` on subsequent, then `Loading speaker diarization pipeline...`. No opaque "Loading..." silences for 2 minutes.
- **CPU-mode chip** — amber `CPU mode` badge + tooltip with the fix hint if Whisper is stuck on CPU despite the app expecting a GPU.

### Error handling / diagnostics
- **Mic permission detection** — `PortAudioError` on stream-open is translated to a structured 403 with `kind=permission`. The frontend shows a modal with a one-click "Open Windows mic settings" button that launches `ms-settings:privacy-microphone`.
- **React error boundary** — a blank-screen render error becomes a recoverable card with the message, component stack, and a Reload button. Your recording keeps running in the sidecar.
- **Sidecar crash dumps** — `sys.excepthook` writes `crash-YYYYMMDD-HHMMSS.log` under `APP_DATA/logs/` with full traceback before exit.
- **Rust panic hook** — panics during Tauri startup write `crash-<unix-secs>-rust.log` to `%APPDATA%\AuraScribe\logs\` alongside the Python crashes.
- **Rotating file log** — `APP_DATA/logs/sidecar.log` keeps 5× 5 MB files of everything the sidecar logs — plenty for debugging recent issues.
- **Startup heartbeat poll** — the UI pings `/api/status` every 30s after the engine is ready. If the sidecar crashes, the header flips to `Sidecar unreachable` instead of looking healthy but being dead.
- **Startup extras check** — on boot the sidecar logs which optional extras (`asr` / `diarization` / `llm`) are installed vs missing, with install-command hints for the missing ones.

### Storage & integrations
- **SQLite** — meetings, utterances (with pyannote embeddings), voices, voice-embedding snippets. Lives in `%APPDATA%\AuraScribe\aurascribe.db` by default (override via Settings → Data directory).
- **Opus audio** — every meeting records a per-meeting `.opus` file (16 kHz, 24 kbps mono) under `APP_DATA/audio/`. Powers click-to-play and the Recompute endpoint.
- **Obsidian vault** — meeting notes, people notes, and daily briefs written under `<vault>/AuraScribe/{Meetings,People,Daily}`, organized `YYYY/MM/*.md`. Throttled live-updates during recording (15s or every 5 chunks, whichever trips first) so Obsidian sync watchers don't thrash.
- **WebSocket feed** — single `/ws` channel broadcasts utterances, partials, status events, real-time intelligence, and daily-brief state changes.

---

## Configuration

Everything lives under a single **data directory** — pick it in Settings (default `%APPDATA%\AuraScribe`). The folder holds the SQLite database, per-meeting Opus recordings, Whisper model cache, logs, editable prompts, and `config.json` with your user settings. Copy that folder to a new machine or pass it to a fresh install to pick up where you left off.

User-editable knobs (all in Settings UI, persisted to `config.json`):

| Key | Default | Purpose |
|---|---|---|
| `hf_token` | — | HuggingFace token for pyannote downloads |
| `llm_base_url` | `http://127.0.0.1:1234/v1` | Root of the `/v1/chat/completions` endpoint |
| `llm_api_key` | `lm-studio` | Provider API key (any non-empty string for LM Studio) |
| `llm_model` | `local-model` | Model id the provider expects (`gpt-4o`, `gemini-2.0-flash`, etc.) |
| `llm_context_tokens` | `4096` | Total context budget of the chosen model |
| `whisper_model` | auto-detected | `large-v3-turbo` (GPU) or `small` (CPU). Any faster-whisper model id accepted. |
| `whisper_device` | auto-detected | `cuda` if CUDA GPU present, else `cpu`. User-overridable. |
| `whisper_compute_type` | auto-detected | `float16` (≥8 GB VRAM), `int8_float16` (4-8 GB), `int8` (CPU or <4 GB) |
| `whisper_language` | `en` | ISO code or empty for auto-detect |
| `my_speaker_label` | `Me` | How your voice is labelled in transcripts |
| `obsidian_vault` | — | Vault root; empty disables Obsidian writes |
| `rt_highlights_debounce_sec` | `20` | Seconds after last utterance before an intel call fires |
| `rt_highlights_max_interval_sec` | `60` | Hard cap between intel calls during continuous speech |
| `rt_highlights_window_sec` | `180` | Transcript window the intel LLM sees |

Settings UI shows the detected hardware next to the Speech section (e.g. `Detected: cuda · NVIDIA GeForce RTX 4090 · 24 GB VRAM`) and marks each override with a `custom` or `default` chip so you can see at a glance which values came from you vs the auto-detect.

A value change isn't live until the sidecar restarts — the Settings UI shows a "restart to apply" banner when a save diverges from the running process.

Sidecar bind address is `SIDECAR_HOST` / `SIDECAR_PORT` env vars read by `main.py` (deployment concern, not a user setting) — defaults to `127.0.0.1:8765`.

---

## Architecture

```
Tauri shell (Rust + WebView2)
    │
    ├── renders → React 19 UI (Vite + Tailwind + lucide-react + @fontsource/inter)
    │                 │
    │                 └── HTTP + WebSocket → Python sidecar (127.0.0.1:8765)
    │                                              │
    │                                              ├── faster-whisper       (ASR, CTranslate2; CUDA or CPU)
    │                                              ├── pyannote.audio 3.1   (diarization + 256-dim embeddings)
    │                                              ├── sounddevice / scipy  (mic capture, VAD, chunking)
    │                                              ├── soundfile            (Opus wall-clock recorder)
    │                                              ├── LLM client           (OpenAI-compat)
    │                                              ├── SQLite (aiosqlite)   (meetings, voices, utterances, embeddings)
    │                                              └── Obsidian writer      (Meetings/People/Daily markdown)
    │
    ├── spawns/kills Python sidecar subprocess
    ├── panics → %APPDATA%\AuraScribe\logs\crash-*-rust.log
    └── rfd::MessageDialog on startup failure (sidecar spawn, Tauri init)
```

The sidecar binds to `127.0.0.1` only. CORS is locked to the Vite dev origin + Tauri webview schemes — external clients can't reach it. On Windows, the `nvidia-*-cu12` pip packages' `bin/` directories are registered via `os.add_dll_directory` at import time so CTranslate2 finds CUDA 12 runtimes without touching the system PATH.

---

## Project layout

```
aurascribe/
├── .github/workflows/
│   └── release.yml                   CI: matrix CUDA/CPU builds → draft release
├── src-tauri/                        Rust shell (Tauri 2)
│   ├── src/lib.rs                    Sidecar spawn/kill, dev/prod path resolution,
│   │                                 panic hook → crash log, fatal error dialogs
│   └── tauri.conf.json               Window + NSIS bundle config
├── src/                              React 19 + Vite + Tailwind
│   ├── App.tsx                       Root state, heartbeat poll, WS dispatch, page router
│   ├── main.tsx                      Vite entry → <ErrorBoundary><App /></ErrorBoundary>
│   ├── components/
│   │   ├── Shell.tsx                 Sidebar + Header layout
│   │   ├── Sidebar.tsx               Memoised nav
│   │   ├── Header.tsx                Status chips (WS, LLM, Obsidian, Whisper/Diarize devices)
│   │   ├── RecordingBar.tsx          Start/Stop + mic picker + VU/Waveform + mic-perm modal
│   │   ├── TranscriptView.tsx        Bubble list, follow-tail scroll, click-to-play, assign
│   │   ├── MeetingList.tsx           Virtualised library list (memoised rows)
│   │   ├── TitleSuggestPopover.tsx   AI title + summary piggyback
│   │   ├── VuMeter.tsx / Waveform.tsx Mic-level + waveform (WebAudio)
│   │   ├── Avatar.tsx / Logo.tsx     Pure presentational (React.memo)
│   │   ├── ErrorBoundary.tsx         Class-component render-error catcher
│   │   └── WelcomeDialog.tsx         First-run hardware summary (once per install)
│   ├── pages/                        LiveFeed, MeetingLibrary, Review, Voices, DailyBrief, Settings
│   └── lib/
│       ├── api.ts                    Typed REST client + ApiError + types
│       ├── useWebSocket.ts           Reconnecting WS with cancellation-safe effect
│       ├── useLLMHealth.ts           Slow poll /api/models
│       ├── useClockTick.ts           Interval-based re-render helper
│       └── MicAudioContext.tsx       Shared WebAudio AnalyserNode for VU/Waveform
├── sidecar/                          Python 3.13 backend
│   ├── main.py                       uvicorn entry + logging config + crash excepthook
│   ├── build.ps1                     PyInstaller driver (auto-aligns torch wheel to host GPU)
│   ├── aurascribe-sidecar.spec       PyInstaller onedir config (bundles torch, pyannote, CUDA DLLs)
│   └── aurascribe/
│       ├── __init__.py               CUDA DLL wiring, torchcodec warning silence
│       ├── api.py                    FastAPI app, CORS, lifespan, /ws, /api/status, /api/models,
│       │                             /api/system/open-mic-settings, router mounting
│       ├── config.py                 Hardware probe, data-dir + config.json loader,
│       │                             auto-detected defaults
│       ├── meeting_manager.py        Recording lifecycle, speculative loop, provisional clustering,
│       │                             action-item extractor
│       ├── routes/
│       │   ├── _shared.py            manager singleton, ws_clients, broadcast_lock,
│       │   │                         vault/analysis/deletion/voice helpers
│       │   ├── meetings.py           CRUD + summarize + suggest-title + trim + split + transcript
│       │   │                         + audio + rename-speaker + assign + recompute + intel-refresh
│       │   ├── voices.py             Voice CRUD + merge + snippet-delete
│       │   ├── settings.py           Data-dir + user-config (with auto-detect defaults surfaced)
│       │   ├── daily_brief.py        GET + refresh + `regen_brief_for_meeting` hook
│       │   └── intel.py              Prompt-file list + open + prompt-path
│       ├── audio/capture.py          sounddevice capture, Silero VAD, Opus recorder,
│       │                             MicUnavailableError translation
│       ├── transcription/
│       │   ├── engine.py             Protocol + StubEngine + StageCallback type
│       │   └── whisper.py            faster-whisper + pyannote, voice pool matching,
│       │                             runtime device introspection
│       ├── llm/
│       │   ├── client.py             OpenAI-compat chat wrapper + LLMUnavailableError
│       │   ├── prompts.py            Prompt template loader (seeds APP_DATA/prompts)
│       │   ├── analysis.py           Combined title + summary pass
│       │   ├── realtime.py           Debounced live-intel loop
│       │   ├── daily_brief.py        Daily rollup generator
│       │   ├── sampling.py           Utterance window sampling
│       │   ├── live_intelligence.md  User-editable
│       │   └── daily_brief.md        User-editable
│       ├── obsidian/writer.py        Meeting / people / daily-brief markdown writers
│       └── db/database.py            SQLite schema + migrations
├── package.json                      Node (frontend + Tauri CLI)
│                                     Scripts: dev, build, tauri:dev, tauri:build,
│                                     build:sidecar, package (= build:sidecar + tauri:build)
└── vite.config.ts                    Dev proxy + manual vendor chunks (react, icons, fonts)
```

---

## REST / WebSocket surface

The sidecar exposes a JSON API plus a single WebSocket for push events. Each feature area lives under its own router module — see [sidecar/aurascribe/routes/](sidecar/aurascribe/routes/) for the authoritative list. The headlines:

### Meetings (`/api/meetings/*`)
- `POST /start` `{ title, device }` — begin recording (`403 {kind:"permission"|"unknown"}` on mic failure)
- `POST /stop` `{ summarize }` — stop, finalize, optionally summarize
- `GET /` `?limit=&offset=&days=` — recent meetings
- `POST /bulk-delete` `{ ids }` · `DELETE /all?days=` — clear
- `GET /{id}` · `DELETE /{id}` · `PATCH /{id}` — read / remove / rename
- `GET /{id}/transcript` — utterances only
- `GET /{id}/audio` — Opus stream (HTTP Range supported)
- `POST /{id}/summarize` · `POST /{id}/suggest-title` — analysis passes
- `POST /{id}/trim` `{ before, after }` · `POST /{id}/split` `{ at, new_title }`
- `POST /{id}/rename-speaker` — bulk rename + embedding fold-in
- `POST /{id}/utterances/{uid}/assign` — tag one line
- `POST /{id}/intel/refresh` — force a real-time intel pass
- `POST /{id}/recompute` — re-diarize a past meeting against current Voices

### Voices (`/api/voices/*`)
- `GET /` · `GET /{id}` — list / detail
- `PATCH /{id}` · `DELETE /{id}` — rename / recolour / delete
- `DELETE /{id}/snippets/{sid}` — remove one embedding from the pool
- `POST /merge` `{ from_id, into_id }` — fold one Voice into another

### Settings (`/api/settings/*`)
- `GET/PUT /data-dir` — the APP_DATA pointer
- `GET/PUT /config` — all user-editable knobs; response includes `requires_restart`

### Daily Brief (`/api/daily-brief/*`)
- `GET /?date=YYYY-MM-DD` — cached brief + meeting list for the day
- `POST /refresh?date=` — force regen (blocks until done)

### Intel (`/api/intel/*`)
- `GET /prompts` · `POST /open-prompt` `{ filename }` — list + open user-editable prompt files
- `GET /prompt-path` — absolute path of the realtime-intel prompt

### System
- `GET /status` — engine readiness + audio devices + hardware probe + `asr` + `diarization`
- `GET /models` — models the configured LLM provider reports
- `POST /system/open-mic-settings` — launches `ms-settings:privacy-microphone`

### WebSocket
- `WS /ws` — push channel: `utterances`, `partial_utterance`, `status`, `realtime_intelligence`, `daily_brief_updated`

---

## Building installers

### Locally

```powershell
npm run build:sidecar   # PyInstaller onedir bundle (10-15 min, ~1.5 GB output)
npm run tauri:build     # Tauri build + NSIS installer
# or both in sequence:
npm run package
```

`build:sidecar` auto-aligns your venv's torch wheel to the host GPU before running PyInstaller — `nvidia-smi` detects the GPU; `torch.__version__` is parsed for the current variant; mismatches trigger a `pip install --index-url cu128` (or `cpu`). Skipped in CI (where the matrix step pins the wheel upfront).

### Via CI (recommended)

Push a tag: `git tag v0.2.0 && git push origin v0.2.0`. [.github/workflows/release.yml](.github/workflows/release.yml) then:

1. Builds **both** variants in parallel on `windows-latest` (matrix `[cuda, cpu]`).
2. Renames outputs to stable filenames — `AuraScribe-CUDA-setup.exe` / `AuraScribe-CPU-setup.exe`.
3. Uploads each as a CI artifact.
4. A follow-up `release` job (tag-only) drafts a GitHub Release with both installers attached + a picker table in the body.

You review the draft, click Publish.

`workflow_dispatch` (manual trigger) runs the builds but skips release creation — useful for testing the pipeline without cutting a tag.

---

## Logs & diagnostics

Everything the sidecar logs — including uvicorn's request log — goes to two places:

- **stdout** — picked up by the Tauri dev console and the packaged `.exe`'s attached console window.
- **file** — `%APPDATA%\AuraScribe\logs\sidecar.log`, rotating at 5 × 5 MB.

Unhandled exceptions on either side drop a crash file in the same folder:

- `crash-YYYYMMDD-HHMMSS.log` — Python sidecar (traceback + timestamp).
- `crash-<unix-secs>-rust.log` — Rust shell (panic payload + source location).

Common prefixes to grep for:
- `provisional:` — in-memory speaker clustering decisions.
- `voice-match:` — enrolled-voice matching (distance, second-best, threshold).
- `diarize:` — pyannote turn boundaries per chunk.
- `asr:` — device + model + compute summary at startup.
- `daily_brief:` — daily-brief regen lifecycle.
- `extras:` — optional-extras availability at boot.

Frontend console: DevTools in the Tauri webview (right-click → Inspect in dev; devtools enabled in debug builds).

---

## Roadmap

- **Done** — Tauri + React + sidecar scaffold; SQLite + config + LLM client + Obsidian writer; live transcription with speculative partials; pyannote 3.1 diarization; tag-as-you-go Voices + provisional clustering + per-utterance re-assignment; recompute; real-time intelligence (highlights, action items, support coaching); editable prompts; daily brief aggregation; YYYY/MM vault structure; meeting trim + split; mid-recording re-adoption; NSIS installer + PyInstaller sidecar bundling; CI matrix for CUDA/CPU variants; hardware auto-detect + user-configurable device/compute; first-run welcome dialog; file-based logs + crash dumps; mic-permission detection; error boundaries; follow-tail scroll pinning; speaker tag popover with contains-query search + meeting roster chips; persistent globally-unique speaker colors (palette slot stored on `voices.color`); Voices-page color swatch picker + custom avatar upload.
- **Next** — WASAPI loopback capture for system audio — capture the speaker mix (Zoom / Teams / Google Meet participants) + the local mic in parallel and merge the two streams before diarization. Loopback alone misses the local user's voice, so both sources are required.
- **Next** — Code-signing for the installer (kill the "Unknown publisher" SmartScreen warning).
- **Backlog** — NVIDIA Parakeet-TDT 0.6B v3 as an optional ASR backend (currently #1 on Open ASR Leaderboard). Needs NeMo, which is painful on Windows — deferred.
- **Backlog** — Multi-vault / per-meeting vault routing.
- **Backlog** — Scheduled meeting pre-briefs (pull upcoming calendar entries + relevant prior meetings into a pre-meeting card).
- **Backlog** — Structured telemetry (opt-in Sentry or local crash reporter upload).
- **Backlog** — MCP server — expose meetings / voices / intel over Model Context Protocol so external agents (Claude Desktop, coding assistants, etc.) can query the local corpus and pull relevant context on demand.
- **Backlog** — Mic test panel — live input-level meter + short record/playback loop in Settings (or the first-run dialog) so the user can verify device + gain before starting a meeting.

---

## Development notes

- **Sidecar-only work** — `.venv\Scripts\python sidecar\main.py` brings up the FastAPI server without Tauri. Hit `http://127.0.0.1:8765/api/status` to verify.
- **Frontend-only work** — `npm run dev` runs Vite standalone on port 1420 with a `/api` + `/ws` proxy to 8765. The WS client reconnects automatically when a sidecar appears.
- **Routes refactor** — `api.py` is now just FastAPI wiring (CORS + lifespan + WebSocket + `/api/status` + `/api/models`). All feature endpoints live in `sidecar/aurascribe/routes/*.py`; shared state + cross-router helpers live in `routes/_shared.py`. Add a new feature area by creating `routes/<name>.py` with an `APIRouter` named `router` and importing it in `routes/__init__.py`.
- **CUDA torch locally** — your dev venv defaults to the CPU torch wheel from PyPI. For GPU-accelerated pyannote (ctranslate2 / faster-whisper have their own CUDA path), run `npm run build:sidecar` once to auto-swap, or do it manually:
  ```
  .venv\Scripts\pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
  ```
- **Rust panics in dev** — show up as an error dialog + a crash file under `%APPDATA%\AuraScribe\logs\`. Release builds exit cleanly on panic after showing the dialog.
- **Adding a new config key** — four edits: `_CONFIG_KEYS` in `config.py`, module-level getter in same file, `_CONFIG_FIELDS` + `_effective_for` + `UserConfigUpdate` in `routes/settings.py`, `ConfigKey` union + Settings UI field in the frontend.
