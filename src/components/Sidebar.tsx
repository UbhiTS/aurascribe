import { memo } from "react";
import { Activity, FolderClosed, Calendar, Settings as SettingsIcon, Users } from "lucide-react";
import { Logo } from "./Logo";

export type Page = "live" | "library" | "review" | "daily" | "voices" | "settings";

interface Props {
  page: Page;
  onNavigate: (p: Page) => void;
}

const NAV: { id: Page; label: string; icon: typeof Activity }[] = [
  { id: "live", label: "Live Feed", icon: Activity },
  { id: "library", label: "Meeting Library", icon: FolderClosed },
  { id: "daily", label: "Daily Briefs", icon: Calendar },
  { id: "voices", label: "Voices", icon: Users },
  { id: "settings", label: "Settings", icon: SettingsIcon },
];

// Sidebar re-renders on every Shell prop change (WS connects, LLM status polls,
// splash state). Pure on `{page, onNavigate}`, so memo elides the work —
// callers already pass a stable onNavigate (setPage from useState).
export const Sidebar = memo(function Sidebar({ page, onNavigate }: Props) {
  return (
    <nav className="w-52 flex-shrink-0 bg-gray-950 border-r border-gray-800 flex flex-col">
      {/* Brand */}
      <div className="flex items-center gap-2.5 px-4 py-4 border-b border-gray-800/60">
        <Logo size={36} />
        <span className="text-gray-100 tracking-tight text-base">
          <span className="font-bold">Aura</span>
          <span className="font-normal">Scribe</span>
        </span>
      </div>

      <div className="flex-1 p-2 space-y-1">
        {NAV.map(({ id, label, icon: Icon }) => {
          const active = page === id || (id === "library" && page === "review");
          return (
            <button
              key={id}
              onClick={() => onNavigate(id)}
              className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all ${
                active
                  ? "bg-gradient-to-r from-brand-500/20 to-purple-500/10 text-gray-100 ring-1 ring-brand-500/40 shadow-inner"
                  : "text-gray-400 hover:text-gray-200 hover:bg-gray-900"
              }`}
            >
              <Icon size={15} className={active ? "text-brand-400" : ""} />
              <span className="truncate">{label}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
});
