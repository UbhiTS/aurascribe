import { useEffect, useState, useRef, useCallback } from "react";
import { Clock, FileText, Loader, CheckSquare, Square, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import type { Meeting } from "../lib/api";

const PAGE_SIZE = 20;
const DAY_OPTIONS = [1, 2, 3, 5, 7];

interface Props {
  selectedId: number | null;
  activeMeetingId: number | null;
  onSelect: (id: number) => void;
  onDeleted: (ids: number[]) => void;
  refreshKey: number;
}

type ConfirmMode = "selected" | "all" | null;

export function MeetingList({ selectedId, activeMeetingId, onSelect, onDeleted, refreshKey }: Props) {
  const [meetings, setMeetings]       = useState<Meeting[]>([]);
  const [days, setDays]               = useState(2);
  const [selected, setSelected]       = useState<Set<number>>(new Set());
  const [loading, setLoading]         = useState(false);
  const [confirmMode, setConfirmMode] = useState<ConfirmMode>(null);
  const [deleting, setDeleting]       = useState(false);

  // Refs so loadPage is stable and never goes stale
  const loadingRef  = useRef(false);
  const offsetRef   = useRef(0);
  const hasMoreRef  = useRef(false);
  const daysRef     = useRef(days);
  const sentinelRef = useRef<HTMLDivElement>(null);

  const loadPage = useCallback(async (reset: boolean) => {
    if (loadingRef.current) return;
    if (!reset && !hasMoreRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    try {
      const off = reset ? 0 : offsetRef.current;
      const items = await api.meetings.list(daysRef.current, PAGE_SIZE, off);
      const more = items.length === PAGE_SIZE;
      offsetRef.current = off + items.length;
      hasMoreRef.current = more;
      setMeetings(prev => reset ? items : [...prev, ...items]);
    } catch {
      // network error — leave existing list
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  // Reset + reload when days filter or refreshKey changes
  useEffect(() => {
    daysRef.current = days;
    offsetRef.current = 0;
    hasMoreRef.current = true;
    setSelected(new Set());
    loadPage(true);
  }, [days, refreshKey, loadPage]);

  // Infinite scroll — fire loadPage when sentinel comes into view
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => { if (entries[0].isIntersecting) loadPage(false); },
      { rootMargin: "120px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [loadPage]);

  // ── Selection helpers ────────────────────────────────────────────────────────

  const deletable = (id: number) => id !== activeMeetingId;
  const deletableMeetings = meetings.filter(m => deletable(m.id));
  const allSelected = deletableMeetings.length > 0 && selected.size === deletableMeetings.length;

  const toggleSelect = (id: number) => {
    if (!deletable(id)) return;
    setSelected(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(deletableMeetings.map(m => m.id)));

  // ── Delete handlers ──────────────────────────────────────────────────────────

  const handleDeleteSelected = async () => {
    // Belt-and-suspenders: strip the active meeting even if somehow selected
    const ids = [...selected].filter(deletable);
    if (!ids.length) { setConfirmMode(null); return; }
    setDeleting(true);
    try {
      await api.meetings.bulkDelete(ids);
      setMeetings(prev => prev.filter(m => !ids.includes(m.id)));
      setSelected(new Set());
      onDeleted(ids);
    } finally {
      setDeleting(false);
      setConfirmMode(null);
    }
  };

  const handleClearAll = async () => {
    // Exclude the active recording from deletion
    const ids = deletableMeetings.map(m => m.id);
    if (!ids.length) { setConfirmMode(null); return; }
    setDeleting(true);
    try {
      await api.meetings.bulkDelete(ids);
      setMeetings(prev => prev.filter(m => !ids.includes(m.id)));
      setSelected(new Set());
      onDeleted(ids);
    } finally {
      setDeleting(false);
      setConfirmMode(null);
    }
  };

  // ── Grouping ─────────────────────────────────────────────────────────────────

  const grouped = meetings.reduce<Record<string, Meeting[]>>((acc, m) => {
    const date = m.started_at.slice(0, 10);
    (acc[date] ??= []).push(m);
    return acc;
  }, {});
  const sortedDates = Object.keys(grouped).sort((a, b) => b.localeCompare(a));

  return (
    <>
      <div className="flex flex-col h-full">

        {/* ── Header ─────────────────────────────────────────────────────────── */}
        <div className="px-3 py-2.5 border-b border-gray-800 space-y-2 flex-shrink-0">
          <div className="flex items-center justify-between">
            <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">Meetings</h2>
            <div className="flex items-center gap-2">
              <select
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                className="text-xs text-gray-400 bg-gray-900 border border-gray-700 rounded px-1.5 py-0.5 outline-none cursor-pointer"
              >
                {DAY_OPTIONS.map(d => (
                  <option key={d} value={d}>{d}d</option>
                ))}
              </select>
            </div>
          </div>

          {meetings.length > 0 && (
            <div className="flex items-center gap-2">
              <button
                onClick={toggleAll}
                className="text-gray-500 hover:text-gray-300 transition-colors flex-shrink-0"
                title={allSelected ? "Deselect all" : "Select all"}
              >
                {allSelected
                  ? <CheckSquare size={13} className="text-brand-400" />
                  : <Square size={13} />}
              </button>
              <span className="text-xs text-gray-600 flex-1 truncate">
                {selected.size > 0 ? `${selected.size} selected` : "Select meetings"}
              </span>
              {selected.size > 0 && (
                <button
                  onClick={() => setConfirmMode("selected")}
                  className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 flex-shrink-0 transition-colors"
                >
                  <Trash2 size={11} />
                  Delete
                </button>
              )}
              <button
                onClick={() => setConfirmMode("all")}
                className="text-xs text-gray-600 hover:text-red-400 flex-shrink-0 transition-colors"
                title="Clear all meetings in current date range"
              >
                Clear all
              </button>
            </div>
          )}
        </div>

        {/* ── List ───────────────────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto scrollbar-thin">
          {sortedDates.length === 0 && !loading && (
            <p className="text-xs text-gray-600 px-4 py-6 text-center">
              No meetings in last {days} day{days !== 1 ? "s" : ""}
            </p>
          )}

          {sortedDates.map(date => (
            <div key={date}>
              <div className="px-3 py-1.5 text-xs text-gray-500 font-medium sticky top-0 bg-gray-950/95 backdrop-blur">
                {new Date(date + "T12:00:00").toLocaleDateString(undefined, {
                  weekday: "short", month: "short", day: "numeric",
                })}
              </div>

              {grouped[date].map(m => (
                <div
                  key={m.id}
                  className={`flex items-stretch border-l-2 transition-colors hover:bg-gray-900 ${
                    selectedId === m.id ? "border-brand-500 bg-gray-900" : "border-transparent"
                  } ${selected.has(m.id) ? "bg-gray-900/50" : ""}`}
                >
                  {/* Checkbox — disabled for the active recording */}
                  <button
                    onClick={(e) => { e.stopPropagation(); toggleSelect(m.id); }}
                    disabled={!deletable(m.id)}
                    title={!deletable(m.id) ? "Cannot delete active recording" : undefined}
                    className="pl-3 pr-1.5 flex items-center transition-colors flex-shrink-0 disabled:opacity-30 disabled:cursor-not-allowed text-gray-600 hover:text-gray-400 disabled:hover:text-gray-600"
                  >
                    {selected.has(m.id)
                      ? <CheckSquare size={13} className="text-brand-400" />
                      : <Square size={13} />}
                  </button>

                  {/* Meeting row */}
                  <button
                    onClick={() => onSelect(m.id)}
                    className="flex-1 text-left px-2 py-2 min-w-0"
                  >
                    <div className="flex items-center gap-1.5">
                      {m.status === "recording"  && <div className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse flex-shrink-0" />}
                      {m.status === "processing" && <Loader size={10} className="animate-spin text-amber-400 flex-shrink-0" />}
                      {m.status === "done"       && <FileText size={10} className="text-gray-600 flex-shrink-0" />}
                      <span className="text-sm text-gray-200 truncate font-medium">{m.title}</span>
                    </div>
                    <div className="flex items-center gap-1 mt-0.5 text-xs text-gray-500">
                      <Clock size={10} />
                      {m.started_at.slice(11, 16)}
                      {m.ended_at && ` – ${m.ended_at.slice(11, 16)}`}
                    </div>
                  </button>
                </div>
              ))}
            </div>
          ))}

          {/* Infinite scroll sentinel */}
          <div ref={sentinelRef} className="py-3 flex justify-center">
            {loading && <Loader size={13} className="animate-spin text-gray-700" />}
          </div>
        </div>
      </div>

      {/* ── Confirmation dialog ───────────────────────────────────────────────── */}
      {confirmMode !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl p-5 w-80">
            {confirmMode === "selected" ? (
              <>
                <h3 className="text-sm font-semibold text-gray-100 mb-1">
                  Delete {selected.size} meeting{selected.size !== 1 ? "s" : ""}?
                </h3>
                <p className="text-xs text-gray-400 mb-4 leading-relaxed">
                  This cannot be undone. Obsidian vault files will not be removed.
                </p>
              </>
            ) : (
              <>
                <h3 className="text-sm font-semibold text-gray-100 mb-1">
                  Clear all meetings?
                </h3>
                <p className="text-xs text-gray-400 mb-4 leading-relaxed">
                  {deletableMeetings.length} meeting{deletableMeetings.length !== 1 ? "s" : ""} from the last {days} day{days !== 1 ? "s" : ""} will be permanently deleted.
                  {activeMeetingId && " The active recording will be kept."}
                  {" "}Obsidian vault files will not be removed.
                </p>
              </>
            )}
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setConfirmMode(null)}
                disabled={deleting}
                className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 rounded-lg hover:bg-gray-800 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={confirmMode === "selected" ? handleDeleteSelected : handleClearAll}
                disabled={deleting}
                className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg transition-colors"
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
