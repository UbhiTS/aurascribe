import { useEffect, useState } from "react";
import { Cpu, Zap, Settings as SettingsIcon, X } from "lucide-react";
import type { AppStatus } from "../lib/api";

const SEEN_KEY = "aurascribe:welcome-seen:v1";

interface Props {
  hardware: AppStatus["hardware"] | null;
  onOpenSettings: () => void;
}

/** First-run dialog — shown once per install, on the first render where
 *  we have a hardware probe to report. Makes the auto-detected ASR defaults
 *  (device, compute type, model) visible so the user understands why
 *  their experience is fast/slow, and can opt to tweak them upfront.
 *
 *  Dismissal is persisted in localStorage — the Tauri webview keeps this
 *  across restarts, so the dialog appears exactly once per user-machine.
 */
export function WelcomeDialog({ hardware, onOpenSettings }: Props) {
  const [dismissed, setDismissed] = useState<boolean>(
    () => typeof window !== "undefined" && window.localStorage.getItem(SEEN_KEY) === "1",
  );

  useEffect(() => {
    if (dismissed) {
      try {
        window.localStorage.setItem(SEEN_KEY, "1");
      } catch {
        // localStorage can throw in private-mode contexts; safe to ignore.
      }
    }
  }, [dismissed]);

  if (dismissed || !hardware) return null;

  const isGpu = hardware.device === "cuda";
  const suggestedModel = isGpu ? "large-v3-turbo" : "small";
  const suggestedCompute = isGpu
    ? hardware.vram_gb != null && hardware.vram_gb < 8 ? "int8_float16" : "float16"
    : "int8";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="bg-gray-900 border border-gray-700 rounded-2xl shadow-2xl p-6 w-[30rem] relative">
        <button
          onClick={() => setDismissed(true)}
          className="absolute top-4 right-4 text-gray-500 hover:text-gray-300"
          title="Dismiss"
        >
          <X size={14} />
        </button>

        <h2 className="text-lg font-semibold text-gray-100 mb-1">
          Welcome to AuraScribe
        </h2>
        <p className="text-xs text-gray-400 leading-relaxed mb-4">
          We probed your machine and picked speech-recognition defaults that
          should run well out of the box. Change any of this in{" "}
          <span className="text-gray-300">Settings → Speech & Transcription</span>.
        </p>

        <div className="space-y-3 mb-5">
          <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-3">
            <div className="flex items-center gap-2 mb-1">
              {isGpu ? (
                <Zap size={12} className="text-emerald-400" />
              ) : (
                <Cpu size={12} className="text-amber-400" />
              )}
              <span className="text-[10px] uppercase tracking-wider text-gray-500">
                Detected
              </span>
            </div>
            <div className="text-sm text-gray-200 font-mono">
              {isGpu
                ? `${hardware.device_name ?? "CUDA GPU"}${hardware.vram_gb ? ` · ${hardware.vram_gb} GB VRAM` : ""}`
                : "CPU only (no CUDA GPU)"}
            </div>
          </div>

          <div className="rounded-lg border border-gray-800 bg-gray-950/50 p-3 space-y-1.5">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">
              Defaults chosen for you
            </div>
            <div className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
              <span className="text-gray-500">Model</span>
              <span className="text-gray-200 font-mono">{suggestedModel}</span>
              <span className="text-gray-500">Device</span>
              <span className="text-gray-200 font-mono">{hardware.device}</span>
              <span className="text-gray-500">Precision</span>
              <span className="text-gray-200 font-mono">{suggestedCompute}</span>
            </div>
          </div>

          {!isGpu && (
            <div className="rounded-lg border border-amber-800/50 bg-amber-950/20 p-3">
              <p className="text-xs text-amber-200 leading-relaxed">
                Heads-up: CPU transcription is ~5–10× slower than on a CUDA
                GPU, but still workable for meetings. If you have an NVIDIA
                GPU, install CUDA 12 + matching drivers and restart — Whisper
                will auto-detect it.
              </p>
            </div>
          )}
        </div>

        <div className="flex gap-2 justify-end">
          <button
            onClick={() => {
              setDismissed(true);
              onOpenSettings();
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-gray-300 border border-gray-700 hover:border-gray-500 rounded-lg"
          >
            <SettingsIcon size={11} />
            Open Settings
          </button>
          <button
            onClick={() => setDismissed(true)}
            className="px-4 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 text-white rounded-lg"
          >
            Get started
          </button>
        </div>
      </div>
    </div>
  );
}
