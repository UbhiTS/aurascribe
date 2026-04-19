import { Book, Brain, Cpu, Plug, PlugZap, Users, Zap } from "lucide-react";
import type { LLMHealth } from "../lib/useLLMHealth";
import type { AppStatus } from "../lib/api";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error";

interface Props {
  wsConnected: boolean;
  llm: LLMHealth;
  activeAudioDevice: string | null;
  isRecording: boolean;
  obsidianConfigured: boolean;
  systemStatus: StatusEvent;
  statusMessage: string;
  hardware: AppStatus["hardware"] | null;
  asr: AppStatus["asr"] | null;
  diarization: AppStatus["diarization"] | null;
}

// Visual grammar for the compute chips: a single colour per location so
// green=GPU / amber=CPU / gray=disabled reads consistently at a glance.
function computeChipClass(device: "cuda" | "cpu" | null): string {
  if (device === "cuda") return "text-emerald-300 border-emerald-800/50 bg-emerald-950/30";
  if (device === "cpu") return "text-amber-300 border-amber-800/50 bg-amber-950/30";
  return "text-gray-400 border-gray-800 bg-gray-900/60";
}

function deviceLabel(device: "cuda" | "cpu" | null): string {
  if (device === "cuda") return "GPU";
  if (device === "cpu") return "CPU";
  return "off";
}

function StatusPill({ status, message }: { status: StatusEvent; message: string }) {
  const cfg =
    status === "loading" || status === "processing"
      ? { dot: "bg-amber-500 animate-pulse", ring: "border-amber-800/50 text-amber-400 bg-amber-950/30", text: status === "processing" ? "Processing" : "Loading" }
      : status === "recording"
      ? { dot: "bg-red-500 animate-pulse", ring: "border-red-800/50 text-red-400 bg-red-950/30", text: "Recording" }
      : status === "error"
      ? { dot: "bg-red-500", ring: "border-red-800/50 text-red-400 bg-red-950/30", text: message || "Error" }
      : { dot: "bg-emerald-500", ring: "border-emerald-800/50 text-emerald-400 bg-emerald-950/30", text: "System Ready" };

  return (
    <div className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs border ${cfg.ring}`}>
      <div className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      {cfg.text}
    </div>
  );
}

export function Header({
  wsConnected, llm, activeAudioDevice,
  isRecording, obsidianConfigured, systemStatus, statusMessage,
  hardware, asr, diarization,
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
        <span className="text-gray-500">·</span>
        <span className={`truncate max-w-[220px] ${
          !llm.online ? "text-red-400"
          : aiMisconfigured ? "text-amber-400"
          : "text-gray-300"
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

      {/* Compute-placement chips. Two of them — one per pipeline — so the
          user always knows where Whisper and pyannote are actually running.
          The two can diverge (ctranslate2 has its own CUDA path; torch may
          be CPU-only), which this layout makes obvious. */}
      {asr && (
        <>
          <div className="h-4 w-px bg-gray-800" />
          <div
            className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] border ${computeChipClass(asr.device)}`}
            title={[
              `Whisper · ${asr.model}`,
              `device: ${asr.device.toUpperCase()}`,
              `precision: ${asr.compute_type}`,
              hardware?.device === "cuda" && hardware.device_name
                ? `on ${hardware.device_name}${hardware.vram_gb ? ` (${hardware.vram_gb} GB VRAM)` : ""}`
                : null,
            ].filter(Boolean).join(" · ")}
          >
            {asr.device === "cuda" ? <Zap size={11} /> : <Cpu size={11} />}
            <span className="font-medium">Whisper</span>
            <span className="font-mono opacity-80 truncate max-w-[140px]">{asr.model}</span>
            <span className="opacity-70">·</span>
            <span className="font-semibold">{deviceLabel(asr.device)}</span>
          </div>
        </>
      )}
      {diarization && (
        <>
          <div
            className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] border ${computeChipClass(diarization.device)}`}
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
            <Users size={11} />
            <span className="font-medium">Diarize</span>
            <span className="opacity-70">·</span>
            <span className="font-semibold">{deviceLabel(diarization.device)}</span>
          </div>
        </>
      )}

      <div className="flex-1" />

      <StatusPill status={systemStatus} message={statusMessage} />
    </header>
  );
}
