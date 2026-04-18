import type { ReactNode } from "react";
import { Sidebar, type Page } from "./Sidebar";
import { Header } from "./Header";

type StatusEvent =
  | "loading" | "ready" | "recording" | "processing" | "done" | "error" | "enrolling";

interface Props {
  page: Page;
  onNavigate: (p: Page) => void;
  selectedDeviceName: string | null;
  systemStatus: StatusEvent;
  statusMessage: string;
  obsidianConfigured: boolean;
  children: ReactNode;
}

export function Shell({
  page, onNavigate, selectedDeviceName,
  systemStatus, statusMessage, obsidianConfigured, children,
}: Props) {
  return (
    <div className="h-screen flex bg-gray-950 text-gray-100 overflow-hidden">
      <Sidebar page={page} onNavigate={onNavigate} />
      <div className="flex-1 flex flex-col min-w-0">
        <Header
          selectedDeviceName={selectedDeviceName}
          systemStatus={systemStatus}
          statusMessage={statusMessage}
          obsidianConfigured={obsidianConfigured}
        />
        <main className="flex-1 min-h-0 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
