import { Book, Brain, Cpu, Plug, PlugZap, Users, Zap } from "lucide-react";
import type { LLMHealth } from "../lib/useLLMHealth";
import type { AppStatus, AutoCaptureState } from "../lib/api";
import { AutoCaptureChip } from "./AutoCaptureChip";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error";

interface Props {
  wsConnected: boolean;
  llm: LLMHealth;
  obsidianConfigured: boolean;
  systemStatus: StatusEvent;
  statusMessage: string;
  hardware: AppStatus["hardware"] | null;
  asr: AppStatus["asr"] | null;
  diarization: AppStatus["diarization"] | null;
  autoCaptureState: AutoCaptureState | null;
  setAutoCaptureState: (s: AutoCaptureState | null) => void;
}

// Every header item uses the same visual grammar: a colored icon carries
// the state, gray text carries the label, a middle-dot separator carries
// any secondary detail. Nothing is pilled — we used to mix pills with
// icon+text and it read as "two languages in one bar".

function computeIconClass(device: "cuda" | "cpu" | null): string {
  if (device === "cuda") return "text-emerald-400";
  if (device === "cpu") return "text-amber-400";
  return "text-gray-500";
}

function deviceLabel(device: "cuda" | "cpu" | null): string {
  if (device === "cuda") return "GPU";
  if (device === "cpu") return "CPU";
  return "off";
}

function deviceTextClass(device: "cuda" | "cpu" | null): string {
  if (device === "cuda") return "text-emerald-300";
  if (device === "cpu") return "text-amber-300";
  return "text-gray-500";
}

function StatusIndicator({ status, message }: { status: StatusEvent; message: string }) {
  // The status item is a dot + text instead of an icon + text — the dot
  // stays as the one place in the bar where animation (pulse) is a strong
  // visual affordance for "something is happening right now".
  const cfg =
    status === "loading" || status === "processing"
      ? { dot: "bg-amber-500 animate-pulse", text: "text-amber-300", label: status === "processing" ? "Processing" : "Loading" }
      : status === "recording"
      ? { dot: "bg-red-500 animate-pulse", text: "text-red-300", label: "Recording" }
      : status === "error"
      ? { dot: "bg-red-500", text: "text-red-300", label: message || "Error" }
      : { dot: "bg-emerald-500", text: "text-emerald-300", label: "System Ready" };

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <div className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      <span className={cfg.text}>{cfg.label}</span>
    </div>
  );
}

export function Header({
  wsConnected, llm,
  obsidianConfigured, systemStatus, statusMessage,
  hardware, asr, diarization, autoCaptureState, setAutoCaptureState,
}: Props) {
  // Provider online when the model list is non-empty. Prefer the configured
  // model name for display; fall back to whatever the provider reports.
  const aiLabel = llm.configuredModel || llm.loadedModels[0] || null;
  const aiMisconfigured = !!(
    llm.online && llm.configuredModel &&
    !llm.loadedModels.includes(llm.configuredModel)
  );

  return (
    <header className="flex items-center gap-3 px-4 py-2.5 border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm flex-shrink-0">
      {/* Sidecar WS connection */}
      <div
        className="flex items-center gap-1.5 text-xs"
        title={wsConnected ? "Sidecar connected" : "Reconnecting to sidecar…"}
      >
        {wsConnected
          ? <PlugZap size={13} className="text-emerald-400" />
          : <Plug size={13} className="text-amber-400 animate-pulse" />}
        <span className={wsConnected ? "text-gray-300" : "text-amber-400"}>
          {wsConnected ? "Sidecar" : "Reconnecting"}
        </span>
      </div>

      <div className="h-4 w-px bg-gray-800" />

      {/* AI model provider */}
      <div
        className="flex items-center gap-1.5 text-xs min-w-0"
        title={
          llm.online
            ? aiMisconfigured
              ? `Configured model "${llm.configuredModel}" not reported by the provider. Available: ${llm.loadedModels.join(", ") || "none"}.`
              : `AI provider online. Available: ${llm.loadedModels.join(", ") || "none"}.`
            : "AI provider unreachable — live intelligence and summaries will fail. Check Settings → LLM Provider."
        }
      >
        <Brain
          size={13}
          className={
            !llm.online ? "text-red-400"
            : aiMisconfigured ? "text-amber-400"
            : "text-emerald-400"
          }
        />
        <span className={!llm.online ? "text-red-400" : "text-gray-300"}>AI</span>
        <span className="text-gray-600">·</span>
        <span className={`truncate max-w-[220px] ${
          !llm.online ? "text-red-400"
          : aiMisconfigured ? "text-amber-400"
          : "text-gray-400"
        }`}>
          {llm.online ? (aiLabel ?? "ready") : "offline"}
        </span>
      </div>

      <div className="h-4 w-px bg-gray-800" />

      {/* Obsidian — icon color carries the state, no extra label */}
      <div
        className="flex items-center gap-1.5 text-xs"
        title={
          obsidianConfigured
            ? "Obsidian vault configured — meetings are mirrored to markdown"
            : "Obsidian vault not set — meetings won't be written to markdown. Configure in Settings → Obsidian."
        }
      >
        <Book
          size={13}
          className={obsidianConfigured ? "text-emerald-400" : "text-red-400"}
        />
        <span className={obsidianConfigured ? "text-gray-300" : "text-red-400"}>
          Obsidian
        </span>
      </div>

      {/* Compute-placement items. One per pipeline so the user always
          knows where Whisper and pyannote are running — they can diverge
          (ctranslate2 has its own CUDA path; torch may be CPU-only). */}
      {asr && (
        <>
          <div className="h-4 w-px bg-gray-800" />
          <div
            className="flex items-center gap-1.5 text-xs"
            title={[
              `Whisper · ${asr.model}`,
              `device: ${asr.device.toUpperCase()}`,
              `precision: ${asr.compute_type}`,
              hardware?.device === "cuda" && hardware.device_name
                ? `on ${hardware.device_name}${hardware.vram_gb ? ` (${hardware.vram_gb} GB VRAM)` : ""}`
                : null,
            ].filter(Boolean).join(" · ")}
          >
            {asr.device === "cuda"
              ? <Zap size={13} className={computeIconClass(asr.device)} />
              : <Cpu size={13} className={computeIconClass(asr.device)} />}
            <span className="text-gray-300">Whisper</span>
            <span className="text-gray-600">·</span>
            <span className="text-gray-400 font-mono truncate max-w-[140px]">{asr.model}</span>
            <span className="text-gray-600">·</span>
            <span className={`font-semibold ${deviceTextClass(asr.device)}`}>
              {deviceLabel(asr.device)}
            </span>
          </div>
        </>
      )}
      {diarization && (
        <>
          <div className="h-4 w-px bg-gray-800" />
          <div
            className="flex items-center gap-1.5 text-xs"
            title={
              diarization.enabled
                ? `Speaker diarization · device: ${diarization.device?.toUpperCase()}${
                    diarization.device === "cpu" && asr?.device === "cuda"
                      ? " (install a CUDA torch wheel to move diarization to the GPU)"
                      : ""
                  }`
                : "Speaker diarization is disabled — requires HF_TOKEN + accepting the pyannote license."
            }
          >
            <Users size={13} className={computeIconClass(diarization.device)} />
            <span className="text-gray-300">Diarize</span>
            <span className="text-gray-600">·</span>
            <span className={`font-semibold ${deviceTextClass(diarization.device)}`}>
              {deviceLabel(diarization.device)}
            </span>
          </div>
        </>
      )}

      <div className="flex-1" />

      <AutoCaptureChip state={autoCaptureState} setState={setAutoCaptureState} />

      <div className="h-4 w-px bg-gray-800" />

      <StatusIndicator status={systemStatus} message={statusMessage} />
    </header>
  );
}
