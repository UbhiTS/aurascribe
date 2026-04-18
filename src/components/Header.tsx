import { Cpu, Mic, FolderCheck, FolderX } from "lucide-react";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error" | "enrolling";

interface Props {
  selectedDeviceName: string | null;
  systemStatus: StatusEvent;
  statusMessage: string;
  obsidianConfigured: boolean;
}

function StatusPill({ status, message }: { status: StatusEvent; message: string }) {
  const cfg =
    status === "loading" || status === "processing"
      ? { dot: "bg-amber-500 animate-pulse", ring: "border-amber-800/50 text-amber-400 bg-amber-950/30", text: status === "processing" ? "Processing" : "Loading" }
      : status === "recording"
      ? { dot: "bg-red-500 animate-pulse", ring: "border-red-800/50 text-red-400 bg-red-950/30", text: "Recording" }
      : status === "enrolling"
      ? { dot: "bg-amber-500 animate-pulse", ring: "border-amber-800/50 text-amber-400 bg-amber-950/30", text: message || "Enrolling" }
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

export function Header({ selectedDeviceName, systemStatus, statusMessage, obsidianConfigured }: Props) {
  const deviceLabel = selectedDeviceName || "Default";
  return (
    <header className="flex items-center gap-4 px-4 py-2.5 border-b border-gray-800 bg-gray-950/80 backdrop-blur-sm flex-shrink-0">
      <div className="flex items-center gap-2 text-xs text-gray-400">
        <Cpu size={13} className="text-gray-500" />
        <span className="text-gray-300">CUDA</span>
        <span className="text-gray-500">·</span>
        <span className="text-gray-500">RTX</span>
      </div>
      <div className="h-4 w-px bg-gray-800" />
      <div className="flex items-center gap-1.5 text-xs text-gray-400">
        <Mic size={13} className="text-gray-500" />
        <span className="text-gray-300 truncate max-w-[220px]">{deviceLabel}</span>
      </div>
      <div className="h-4 w-px bg-gray-800" />
      <div className="flex items-center gap-1.5 text-xs text-gray-400">
        {obsidianConfigured
          ? <FolderCheck size={13} className="text-emerald-400" />
          : <FolderX size={13} className="text-gray-500" />}
        <span className="text-gray-300">
          Obsidian: <span className={obsidianConfigured ? "text-emerald-400" : "text-gray-500"}>{obsidianConfigured ? "Configured" : "Not set"}</span>
        </span>
      </div>

      <div className="flex-1" />

      <StatusPill status={systemStatus} message={statusMessage} />
    </header>
  );
}
