import { Brain, FolderCheck, FolderX, Mic, Plug, PlugZap, Radio } from "lucide-react";
import type { LLMHealth } from "../lib/useLLMHealth";

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

      {/* Active recording mic — only while recording */}
      {isRecording && (
        <>
          <div className="h-4 w-px bg-gray-800" />
          <div className="flex items-center gap-1.5 text-xs min-w-0">
            <Radio size={13} className="text-red-400 animate-pulse flex-shrink-0" />
            <Mic size={12} className="text-gray-500 flex-shrink-0" />
            <span
              className="text-gray-400 truncate max-w-[220px]"
              title={activeAudioDevice ?? "Default mic"}
            >
              {activeAudioDevice ?? "Default mic"}
            </span>
          </div>
        </>
      )}

      <div className="flex-1" />

      {/* Obsidian — icon only, detail in tooltip */}
      <div
        className="flex items-center"
        title={obsidianConfigured ? "Obsidian vault configured" : "Obsidian vault not set — meetings won't be written to markdown"}
      >
        {obsidianConfigured
          ? <FolderCheck size={14} className="text-emerald-400" />
          : <FolderX size={14} className="text-gray-600" />}
      </div>

      <StatusPill status={systemStatus} message={statusMessage} />
    </header>
  );
}
