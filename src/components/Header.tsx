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

// Responsive ladder. The window min-width is 960px (Tauri config), so
// below `lg` (1024px) we're in "just above minimum" territory — labels
// start collapsing to icons so the essential bits (AI model, Auto
// Recording toggle, system status) keep full visibility. At `2xl` and
// above everything has generous breathing room.
//
// Every header item uses the same visual grammar: a colored icon carries
// state, gray text carries the label, a middle-dot separator carries any
// secondary detail. Labels that collapse under `xl` fall back to an icon
// with a tooltip — no info is lost, just visual density.

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

// Only renders when something is actually wrong. Every other status
// (Loading / Recording / Processing / Ready) is already conveyed more
// clearly elsewhere in the UI:
//   * Loading    — the Sidecar indicator on the far left flips to
//                  "Reconnecting" when the sidecar isn't ready.
//   * Recording  — the RecordingBar shows its own red pulsing dot +
//                  timer + Stop button.
//   * Processing — transient state the user doesn't need to act on.
//   * Ready      — the green Sidecar indicator already signals health.
// Surfacing errors is the one case the user can't learn about from
// anywhere else, so the pill is reserved for that.
function ErrorIndicator({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-1.5 text-xs flex-shrink-0" title={message || "Error"}>
      <div className="w-1.5 h-1.5 rounded-full bg-red-500" />
      <span className="text-red-300 hidden md:inline">{message || "Error"}</span>
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

  // Shared by every "collapsing" header item — labels hide below `xl`
  // (1280px), tooltip still carries the info.
  const labelCollapsible = "hidden xl:inline";
  const separatorCollapsible = "text-gray-600 hidden xl:inline";

  return (
    <header className="flex items-center gap-2 lg:gap-3 px-3 lg:px-4 py-2.5 border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm flex-shrink-0 overflow-hidden">
      {/* Sidecar WS connection */}
      <div
        className="flex items-center gap-1.5 text-xs flex-shrink-0"
        title={wsConnected ? "Sidecar connected" : "Reconnecting to sidecar…"}
      >
        {wsConnected
          ? <PlugZap size={13} className="text-emerald-400" />
          : <Plug size={13} className="text-amber-400 animate-pulse" />}
        <span className={`${wsConnected ? "text-gray-300" : "text-amber-400"} ${labelCollapsible}`}>
          {wsConnected ? "Sidecar" : "Reconnecting"}
        </span>
      </div>

      <div className="h-4 w-px bg-gray-800 hidden xl:block" />

      {/* AI model provider — the model name truncates progressively so
          narrow windows get a short slug instead of clobbering Whisper. */}
      <div
        className="flex items-center gap-1.5 text-xs min-w-0 flex-shrink"
        title={
          llm.online
            ? aiMisconfigured
              ? `Configured model "${llm.configuredModel}" not reported by the provider. Available: ${llm.loadedModels.join(", ") || "none"}.`
              : `AI provider online. Model: ${aiLabel}. Available: ${llm.loadedModels.join(", ") || "none"}.`
            : "AI provider unreachable — live intelligence and summaries will fail. Check Settings → LLM Provider."
        }
      >
        <Brain
          size={13}
          className={`flex-shrink-0 ${
            !llm.online ? "text-red-400"
            : aiMisconfigured ? "text-amber-400"
            : "text-emerald-400"
          }`}
        />
        <span className={`${!llm.online ? "text-red-400" : "text-gray-300"} flex-shrink-0`}>AI</span>
        <span className="text-gray-600 flex-shrink-0">·</span>
        <span className={`truncate min-w-0 max-w-[80px] lg:max-w-[140px] 2xl:max-w-[220px] ${
          !llm.online ? "text-red-400"
          : aiMisconfigured ? "text-amber-400"
          : "text-gray-400"
        }`}>
          {llm.online ? (aiLabel ?? "ready") : "offline"}
        </span>
      </div>

      <div className="h-4 w-px bg-gray-800 hidden xl:block" />

      {/* Obsidian — label collapses under xl, icon+tooltip survive */}
      <div
        className="flex items-center gap-1.5 text-xs flex-shrink-0"
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
        <span className={`${obsidianConfigured ? "text-gray-300" : "text-red-400"} ${labelCollapsible}`}>
          Obsidian
        </span>
      </div>

      {/* Compute-placement items. Whisper model name collapses first
          (hidden under lg), then the "Whisper" label under xl — icon +
          GPU/CPU badge always survive so the most important signal
          (where is compute happening) is never hidden. */}
      {asr && (
        <>
          <div className="h-4 w-px bg-gray-800 hidden xl:block" />
          <div
            className="flex items-center gap-1.5 text-xs min-w-0 flex-shrink"
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
              ? <Zap size={13} className={`flex-shrink-0 ${computeIconClass(asr.device)}`} />
              : <Cpu size={13} className={`flex-shrink-0 ${computeIconClass(asr.device)}`} />}
            <span className={`text-gray-300 flex-shrink-0 ${labelCollapsible}`}>Whisper</span>
            <span className={separatorCollapsible}>·</span>
            <span className="text-gray-400 font-mono truncate min-w-0 max-w-[100px] 2xl:max-w-[180px] hidden lg:inline">
              {asr.model}
            </span>
            <span className="text-gray-600 hidden lg:inline flex-shrink-0">·</span>
            <span className={`font-semibold flex-shrink-0 ${deviceTextClass(asr.device)}`}>
              {deviceLabel(asr.device)}
            </span>
          </div>
        </>
      )}
      {diarization && (
        <>
          <div className="h-4 w-px bg-gray-800 hidden xl:block" />
          <div
            className="flex items-center gap-1.5 text-xs flex-shrink-0"
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
            <span className={`text-gray-300 ${labelCollapsible}`}>Diarize</span>
            <span className={separatorCollapsible}>·</span>
            <span className={`font-semibold ${deviceTextClass(diarization.device)}`}>
              {deviceLabel(diarization.device)}
            </span>
          </div>
        </>
      )}

      <div className="flex-1 min-w-[8px]" />

      <AutoCaptureChip state={autoCaptureState} setState={setAutoCaptureState} />

      {systemStatus === "error" && (
        <>
          <div className="h-4 w-px bg-gray-800 hidden md:block" />
          <ErrorIndicator message={statusMessage} />
        </>
      )}
    </header>
  );
}
