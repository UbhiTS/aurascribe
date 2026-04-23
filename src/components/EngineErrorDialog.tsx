import { useState } from "react";
import { AlertTriangle, RefreshCw, X } from "lucide-react";
import { api } from "../lib/api";

interface Props {
  error: string;
  // Engine is currently trying to load again (systemStatus flipped back to
  // "loading" after the retry). Swap the Retry button into a spinner.
  reloading: boolean;
  onDismiss: () => void;
}

// Full-screen dialog shown when engine.load() failed during startup (or a
// subsequent retry). Replaces the "spinning forever on Loading…" behavior
// that was the most common first-run bad experience: user sees a blank app
// and force-quits before we can tell them their HF token was rejected.
//
// Common causes this dialog helps the user diagnose:
//   - HuggingFace token not set → 401 on pyannote model download
//   - HF licence not accepted → GatedRepo error
//   - Whisper download interrupted → partial file in cache
//   - GPU OOM when a bigger model tries to load
//   - Missing CUDA DLL (clean reinstall fixes)
export function EngineErrorDialog({ error, reloading, onDismiss }: Props) {
  const [retrying, setRetrying] = useState(false);

  const handleRetry = async () => {
    if (retrying || reloading) return;
    setRetrying(true);
    try {
      await api.system.retryInit();
      // The server starts engine.load() in the background and broadcasts
      // status:loading → ready or status:error via WS. We dismiss this
      // dialog as soon as the status flips (App.tsx watches for that).
      onDismiss();
    } catch (e) {
      console.warn("retry-init failed", e);
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="bg-gray-900 border border-red-800/60 rounded-xl shadow-2xl p-5 w-[28rem] max-w-[90vw]">
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-2">
            <AlertTriangle size={16} className="text-red-400" />
            <h3 className="text-sm font-semibold text-gray-100">
              Transcription models couldn't load
            </h3>
          </div>
          <button
            onClick={onDismiss}
            className="text-gray-500 hover:text-gray-300"
            title="Dismiss — you can reopen this from the header"
          >
            <X size={14} />
          </button>
        </div>
        <p className="text-xs text-gray-300 leading-relaxed mb-2 font-mono bg-black/40 rounded px-2 py-1.5 whitespace-pre-wrap break-words">
          {error}
        </p>
        <p className="text-xs text-gray-400 leading-relaxed mb-4">
          Common fixes: paste your HuggingFace token in Settings and accept
          the licences for <code className="text-gray-300">pyannote/speaker-diarization-3.1</code>,{" "}
          <code className="text-gray-300">pyannote/segmentation-3.0</code>, and{" "}
          <code className="text-gray-300">pyannote/wespeaker-voxceleb-resnet34-LM</code>.
          If the Whisper model download was interrupted, re-running Retry
          usually fixes it.
        </p>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onDismiss}
            className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 rounded-lg hover:bg-gray-800"
          >
            Dismiss
          </button>
          <button
            onClick={handleRetry}
            disabled={retrying || reloading}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white rounded-lg"
          >
            <RefreshCw size={11} className={retrying || reloading ? "animate-spin" : ""} />
            {reloading ? "Reloading…" : retrying ? "Starting…" : "Retry"}
          </button>
        </div>
      </div>
    </div>
  );
}
