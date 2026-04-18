import { useEffect, useMemo, useState } from "react";
import { Search, Clock, FileText, Trash2, Loader, CheckSquare, Square } from "lucide-react";
import { api } from "../lib/api";
import type { Meeting } from "../lib/api";
import { Avatar } from "../components/Avatar";

interface Props {
  activeMeetingId: string | null;
  refreshKey: number;
  onOpen: (id: string) => void;
  selectedId: string | null;
}

const DAY_OPTIONS = [1, 2, 3, 5, 7, 14, 30];

export function MeetingLibrary({ activeMeetingId, refreshKey, onOpen, selectedId }: Props) {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [days, setDays] = useState(7);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const list = await api.meetings.list(days, 200, 0);
      setMeetings(list);
    } catch {
      // leave list as-is
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [days, refreshKey]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return meetings;
    return meetings.filter((m) => m.title.toLowerCase().includes(q));
  }, [meetings, query]);

  const toggle = (id: string) => {
    if (id === activeMeetingId) return;
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const handleBulkDelete = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    setDeleting(true);
    try {
      await api.meetings.bulkDelete(ids);
      setMeetings((m) => m.filter((x) => !selected.has(x.id)));
      setSelected(new Set());
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-800/60">
        <div className="flex-1 relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search transcriptions..."
            className="w-full pl-9 pr-3 py-1.5 text-sm bg-gray-900 border border-gray-800 rounded-lg outline-none focus:border-brand-500 text-gray-200"
          />
        </div>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="text-xs text-gray-300 bg-gray-900 border border-gray-800 rounded px-2 py-1.5 outline-none cursor-pointer"
          title="Date range"
        >
          {DAY_OPTIONS.map((d) => <option key={d} value={d}>Last {d}d</option>)}
        </select>
        {selected.size > 0 && (
          <button
            onClick={handleBulkDelete}
            disabled={deleting}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-red-800/50 text-red-400 bg-red-950/30 hover:bg-red-900/30"
          >
            <Trash2 size={12} />
            Delete {selected.size}
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-5">
        {filtered.length === 0 && !loading && (
          <div className="text-gray-500 text-sm text-center py-12">No transcriptions match.</div>
        )}
        {loading && meetings.length === 0 && (
          <div className="flex items-center justify-center py-12 text-gray-500">
            <Loader size={16} className="animate-spin" />
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4 gap-3">
          {filtered.map((m) => (
            <MeetingCard
              key={m.id}
              m={m}
              active={m.id === activeMeetingId}
              selected={selected.has(m.id)}
              highlighted={selectedId === m.id}
              onToggleSelect={() => toggle(m.id)}
              onOpen={() => onOpen(m.id)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function MeetingCard({
  m, active, selected, highlighted, onToggleSelect, onOpen,
}: {
  m: Meeting; active: boolean; selected: boolean; highlighted: boolean;
  onToggleSelect: () => void; onOpen: () => void;
}) {
  const speakers = useSpeakers(m);
  const takeaways = useTakeaways(m);
  return (
    <div
      className={`relative rounded-xl border p-3.5 transition-all cursor-pointer ${
        highlighted
          ? "border-brand-500/60 bg-brand-950/30 shadow-lg shadow-brand-500/10"
          : "border-gray-800 bg-gray-900/40 hover:border-gray-700 hover:bg-gray-900/70"
      }`}
      onClick={onOpen}
    >
      {!active && (
        <button
          onClick={(e) => { e.stopPropagation(); onToggleSelect(); }}
          className="absolute top-3 right-3 text-gray-500 hover:text-brand-400"
          title="Select"
        >
          {selected ? <CheckSquare size={13} className="text-brand-400" /> : <Square size={13} />}
        </button>
      )}

      <div className="flex items-center gap-1.5">
        {m.status === "recording" && <div className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />}
        {m.status === "processing" && <Loader size={10} className="animate-spin text-amber-400" />}
        {m.status === "done" && <FileText size={10} className="text-gray-500" />}
        <h3 className="text-sm font-semibold text-gray-100 truncate">{m.title}</h3>
      </div>
      <div className="flex items-center gap-1 text-[11px] text-gray-500 mt-0.5">
        <Clock size={10} />
        {m.started_at.slice(11, 16)}{m.ended_at && ` – ${m.ended_at.slice(11, 16)}`}
        <span className="mx-1">·</span>
        {m.started_at.slice(0, 10)}
      </div>

      <div className="flex -space-x-1.5 mt-3">
        {speakers.slice(0, 4).map((s) => <Avatar key={s} name={s} size="sm" className="ring-2 ring-gray-900" />)}
        {speakers.length > 4 && (
          <div className="w-6 h-6 rounded-full bg-gray-800 ring-2 ring-gray-900 flex items-center justify-center text-[9px] text-gray-400">
            +{speakers.length - 4}
          </div>
        )}
      </div>

      {takeaways.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-[11px] text-gray-400 leading-relaxed">
          {takeaways.slice(0, 3).map((t, i) => (
            <li key={i} className="flex gap-1.5 items-start">
              <span className="text-gray-600 mt-1">•</span>
              <span className="line-clamp-1">{t}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function useSpeakers(m: Meeting): string[] {
  const [s, setS] = useState<string[]>([]);
  useEffect(() => {
    api.meetings.get(m.id).then((full) => {
      const u = full.utterances ?? [];
      const unique = [...new Set(u.map((x) => x.speaker))].filter(Boolean);
      setS(unique);
    }).catch(() => {});
  }, [m.id]);
  return s;
}

function useTakeaways(m: Meeting): string[] {
  // Extract the first few bullet lines from Key Decisions or Summary.
  if (!m.summary) return [];
  const lines = m.summary.split("\n");
  const bullets: string[] = [];
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith("- ") || t.startsWith("* ")) {
      bullets.push(t.replace(/^[-*]\s+/, ""));
      if (bullets.length >= 3) break;
    }
  }
  return bullets;
}
