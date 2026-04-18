# AuraScribe sidecar

FastAPI backend for the Tauri shell. Runs as a subprocess spawned by Tauri; binds to `127.0.0.1:8765` by default.

## Dev

From repo root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\sidecar
python sidecar/main.py
```

Then hit `http://127.0.0.1:8765/api/status`.

## Optional extras

```powershell
pip install -e '.\sidecar[asr]'          # faster-whisper + audio capture
pip install -e '.\sidecar[diarization]'  # pyannote (needs PyTorch + CUDA toolkit)
pip install -e '.\sidecar[llm]'          # OpenAI SDK (any OpenAI-compat provider)
pip install -e '.\sidecar[all]'          # everything
```
