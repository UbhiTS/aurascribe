import type { ReactNode } from "react";
import { Sidebar, type Page } from "./Sidebar";
import { Header } from "./Header";
import type { LLMHealth } from "../lib/useLLMHealth";
import type { AppStatus } from "../lib/api";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error";

interface Props {
  page: Page;
  onNavigate: (p: Page) => void;
  wsConnected: boolean;
  llm: LLMHealth;
  activeAudioDevice: string | null;
  isRecording: boolean;
  systemStatus: StatusEvent;
  statusMessage: string;
  obsidianConfigured: boolean;
  hardware: AppStatus["hardware"] | null;
  asr: AppStatus["asr"] | null;
  diarization: AppStatus["diarization"] | null;
  children: ReactNode;
}

export function Shell({
  page, onNavigate, wsConnected, llm,
  activeAudioDevice, isRecording,
  systemStatus, statusMessage, obsidianConfigured, hardware,
  asr, diarization, children,
}: Props) {
  return (
    <div className="h-screen flex bg-gray-950 text-gray-100 overflow-hidden">
      <Sidebar page={page} onNavigate={onNavigate} />
      <div className="flex-1 flex flex-col min-w-0">
        <Header
          wsConnected={wsConnected}
          llm={llm}
          activeAudioDevice={activeAudioDevice}
          isRecording={isRecording}
          systemStatus={systemStatus}
          statusMessage={statusMessage}
          obsidianConfigured={obsidianConfigured}
          hardware={hardware}
          asr={asr}
          diarization={diarization}
        />
        <main className="flex-1 min-h-0 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
