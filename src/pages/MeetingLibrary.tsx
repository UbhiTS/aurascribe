import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Search, Clock, FileText, Trash2, Loader, CheckSquare, Square,
  ChevronLeft, ChevronRight, Sparkles, Speech, Upload,
} from "lucide-react";
import { api } from "../lib/api";
import type { Meeting } from "../lib/api";
import { Avatar } from "../components/Avatar";
import { fmtClockTime } from "../lib/time";
import { useEscapeKey } from "../lib/useEscapeKey";

interface Props {
  activeMeetingId: string | null;
  refreshKey: number;
  onOpen: (id: string) => void;
  selectedId: string | null;
}

type CardAction = "summarize" | "recompute" | "delete";
type BulkAction = CardAction | null;

export function MeetingLibrary({ activeMeetingId, refreshKey, onOpen, selectedId }: Props) {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedDate, setSelectedDate] = useState<string>(todayIso());
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState<BulkAction>(null);
  const [cardBusy, setCardBusy] = useState<Record<string, CardAction | undefined>>({});
  // Which meeting the bulk loop is currently hitting the API for. Drives a
  // ring on the card so the user can see progress sweep top→down.
  const [processingId, setProcessingId] = useState<string | null>(null);
  // Audio import (ffmpeg → opus → transcribe → finalize). Disabled while
  // the upload + transcription is in flight; the file picker re-opens via
  // the hidden <input ref={importInputRef}>.
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  // ID of the most recently imported (or otherwise programmatically focused)
  // meeting — gets a brief amber ring so the user can spot where their
  // import landed, especially when we auto-jump to a different date.
  const [highlightId, setHighlightId] = useState<string | null>(null);
  // Pending delete confirmation. `single` carries the meeting whose card
  // trash button was clicked (we keep the title for a personable prompt);
  // `bulk` carries the snapshot of selected ids at the moment the toolbar
  // Delete was clicked. Null when no modal is showing. Mirrors the
  // sidebar's confirm pattern in MeetingList.tsx.
  type ConfirmDelete =
    | { kind: "single"; id: string; title: string }
    | { kind: "bulk"; ids: string[] };
  const [confirmDelete, setConfirmDelete] = useState<ConfirmDelete | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.meetings.list(2, 200, 0, selectedDate);
      setMeetings(list);
    } catch {
      // leave list as-is
    } finally {
      setLoading(false);
    }
  }, [selectedDate]);

  useEffect(() => { load(); }, [load, refreshKey]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return meetings;
    return meetings.filter((m) => m.title.toLowerCase().includes(q));
  }, [meetings, query]);

  // Stable callbacks so the memoized MeetingCard below doesn't re-render
  // every time the user types in the search box (which rebuilds `filtered`
  // and re-renders this component).
  const toggle = useCallback((id: string) => {
    if (id === activeMeetingId) return;
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }, [activeMeetingId]);

  const selectableIds = useMemo(
    () => filtered.filter((m) => m.id !== activeMeetingId).map((m) => m.id),
    [filtered, activeMeetingId],
  );
  const allSelected =
    selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const handleToggleSelectAll = useCallback(() => {
    if (selected.size > 0) {
      setSelected(new Set());
    } else {
      setSelected(new Set(selectableIds));
    }
  }, [selected.size, selectableIds]);

  const anyBusy = bulkBusy !== null || Object.values(cardBusy).some(Boolean);
  // True while a delete is in flight — used to prevent the modal closing
  // mid-operation (Escape, click-outside, or Cancel button).
  const deleteInFlight =
    bulkBusy === "delete"
    || Object.values(cardBusy).some((a) => a === "delete");
  useEscapeKey(
    () => setConfirmDelete(null),
    confirmDelete !== null && !deleteInFlight,
  );

  // Ref-backed re-entry guard. React state flips on the next render, so a
  // rapid double-click (or an event bubbling through both the card and its
  // icon) could sneak past the state-based `anyBusy` check before the
  // setState commits. The ref flips synchronously — one in-flight action
  // across the whole page, period.
  const actionInFlight = useRef(false);

  // The actual API hit for a single-card delete. Split out from the
  // dispatcher below so the confirm modal can call it on user approval
  // without re-routing through the action switch (which would pop the
  // modal again on the next click).
  const performSingleDelete = useCallback(async (id: string) => {
    if (actionInFlight.current) return;
    actionInFlight.current = true;
    setCardBusy((p) => ({ ...p, [id]: "delete" }));
    try {
      await api.meetings.delete(id);
      setMeetings((m) => m.filter((x) => x.id !== id));
      setSelected((s) => { const n = new Set(s); n.delete(id); return n; });
    } catch {
      // swallow; a future toast system can surface this
    } finally {
      setCardBusy((p) => {
        const next = { ...p };
        delete next[id];
        return next;
      });
      actionInFlight.current = false;
      setConfirmDelete(null);
    }
  }, []);

  const handleCardAction = useCallback(async (id: string, action: CardAction) => {
    // Delete is destructive — route through the confirm modal instead of
    // hitting the API immediately. The other actions stay synchronous.
    if (action === "delete") {
      const m = meetings.find((x) => x.id === id);
      setConfirmDelete({ kind: "single", id, title: m?.title ?? "this meeting" });
      return;
    }
    if (actionInFlight.current) return;
    actionInFlight.current = true;
    setCardBusy((p) => ({ ...p, [id]: action }));
    try {
      if (action === "recompute") {
        await api.meetings.recompute(id);
        // No visible row change from recompute — skip refetch.
      } else if (action === "summarize") {
        const updated = await api.meetings.summarize(id);
        setMeetings((m) => m.map((x) => (x.id === id ? { ...x, ...updated } : x)));
      }
    } catch {
      // swallow; a future toast system can surface this
    } finally {
      setCardBusy((p) => {
        const next = { ...p };
        delete next[id];
        return next;
      });
      actionInFlight.current = false;
    }
  }, [meetings]);

  // Order the current selection newest-first by started_at so the bulk
  // loop processes the most recent meeting first and works backwards —
  // matches the grid's top-to-bottom visual order.
  const orderedSelection = useCallback((): string[] => {
    const sel = selected;
    return meetings
      .filter((m) => sel.has(m.id))
      .sort((a, b) => (a.started_at < b.started_at ? 1 : -1))
      .map((m) => m.id);
  }, [meetings, selected]);

  // Open the confirm modal with a snapshot of the current selection. The
  // ids are captured here so the user can keep clicking around while the
  // modal is up without changing what gets deleted on confirm.
  const handleBulkDelete = useCallback(() => {
    if (actionInFlight.current) return;
    const ids = orderedSelection();
    if (!ids.length) return;
    setConfirmDelete({ kind: "bulk", ids });
  }, [orderedSelection]);

  const performBulkDelete = useCallback(async (ids: string[]) => {
    if (actionInFlight.current || !ids.length) return;
    actionInFlight.current = true;
    setBulkBusy("delete");
    try {
      await api.meetings.bulkDelete(ids);
      const idSet = new Set(ids);
      setMeetings((m) => m.filter((x) => !idSet.has(x.id)));
      setSelected(new Set());
    } finally {
      setBulkBusy(null);
      actionInFlight.current = false;
      setConfirmDelete(null);
    }
  }, []);

  const handleBulkRecompute = useCallback(async () => {
    if (actionInFlight.current) return;
    const ids = orderedSelection();
    if (!ids.length) return;
    actionInFlight.current = true;
    setBulkBusy("recompute");
    try {
      // Sequential newest→oldest so the user can watch the ring sweep
      // down the grid. Recompute is DB-only so latency is trivial; the
      // sequential loop only costs a few ms per meeting but gives the
      // UI a clear progress signal.
      for (const id of ids) {
        setProcessingId(id);
        try {
          await api.meetings.recompute(id);
        } catch {
          // keep going — one failure shouldn't abort the batch
        }
      }
    } finally {
      setProcessingId(null);
      setBulkBusy(null);
      actionInFlight.current = false;
    }
  }, [selected, orderedSelection]);

  const handleBulkSummarize = useCallback(async () => {
    if (actionInFlight.current) return;
    const ids = orderedSelection();
    if (!ids.length) return;
    actionInFlight.current = true;
    setBulkBusy("summarize");
    try {
      // Strictly one-at-a-time, newest→oldest — each call is an LLM
      // round-trip and we never want two inference requests in flight
      // for the same user.
      for (const id of ids) {
        setProcessingId(id);
        try {
          const updated = await api.meetings.summarize(id);
          setMeetings((m) => m.map((x) => (x.id === id ? { ...x, ...updated } : x)));
        } catch {
          // keep going — one failure shouldn't abort the batch
        }
      }
    } finally {
      setProcessingId(null);
      setBulkBusy(null);
      actionInFlight.current = false;
    }
  }, [selected, orderedSelection]);

  const shiftDate = (delta: number) => {
    const next = shiftIso(selectedDate, delta);
    if (delta > 0 && next > todayIso()) return;
    setSelectedDate(next);
    setSelected(new Set());
  };

  const isToday = selectedDate === todayIso();

  const handleImportClick = () => {
    if (importBusy) return;
    importInputRef.current?.click();
  };

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    // Reset input so picking the same filename again still fires onChange.
    e.target.value = "";
    if (!file || importBusy) return;
    setImportBusy(true);
    try {
      const meeting = await api.meetings.importAudio(file);
      // Auto-navigate the date selector if the import landed on a
      // different day (file mtime older than today, etc.) — otherwise
      // the new card would be invisible until the user manually picks
      // the right date. `started_at` is full ISO; trim to YYYY-MM-DD.
      const importedDate = meeting.started_at.slice(0, 10);
      if (importedDate !== selectedDate) {
        setSelected(new Set());
        setSelectedDate(importedDate);
      } else {
        // Same date — just refresh so the new card shows up.
        load();
      }
      // Drop a transient highlight so the user can spot the import,
      // especially after a date jump where the list otherwise looks
      // unchanged. Cleared after 4s.
      setHighlightId(meeting.id);
      window.setTimeout(() => {
        setHighlightId((curr) => (curr === meeting.id ? null : curr));
      }, 4000);
    } catch (err: any) {
      const detail = err?.message || String(err);
      alert(`Import failed:\n\n${detail}`);
    } finally {
      setImportBusy(false);
    }
  };

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-800/60">
        <div className="flex-1 relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search transcriptions..."
            className="w-full pl-9 pr-3 py-1.5 text-sm bg-gray-900 border border-gray-800 rounded-lg outline-none focus:border-brand-500 text-gray-200"
          />
        </div>

        <button
          onClick={() => shiftDate(-1)}
          disabled={loading}
          title="Previous day"
          className="p-1.5 rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          <ChevronLeft size={14} />
        </button>
        <input
          type="date"
          value={selectedDate}
          onChange={(e) => { setSelectedDate(e.target.value); setSelected(new Set()); }}
          max={todayIso()}
          className="px-2.5 py-1.5 text-xs rounded-lg border border-gray-800 bg-gray-900/60 text-gray-200 outline-none focus:border-brand-500"
        />
        <button
          onClick={() => shiftDate(1)}
          disabled={loading || isToday}
          title="Next day"
          className="p-1.5 rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          <ChevronRight size={14} />
        </button>

        {/* Import audio file: ffmpeg-decoded → transcribed → summarized.
            File picker is hidden; the visible button just delegates to it.
            Disabled mid-import so a second file can't queue behind the
            first (transcription holds the engine for tens of seconds). */}
        <input
          ref={importInputRef}
          type="file"
          accept=".opus,.ogg,.wav,.flac,.mp3,.m4a,.aac,.wma,.webm,.mp4,.mkv,.mov,audio/*,video/*"
          className="hidden"
          onChange={handleImportFile}
        />
        <button
          onClick={handleImportClick}
          disabled={importBusy || anyBusy}
          title="Import an existing audio file as a new meeting"
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          {importBusy ? <Loader size={12} className="animate-spin" /> : <Upload size={12} />}
          {importBusy ? "Importing…" : "Import"}
        </button>

        <button
          onClick={handleToggleSelectAll}
          disabled={selectableIds.length === 0 || anyBusy}
          title={selected.size > 0 ? "Clear selection" : "Select all"}
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-gray-800 bg-gray-900/60 text-gray-300 hover:text-gray-100 hover:border-gray-700 transition-colors disabled:opacity-40"
        >
          {allSelected ? <CheckSquare size={12} className="text-brand-400" /> : <Square size={12} />}
          {selected.size > 0 ? `Clear (${selected.size})` : "Select all"}
        </button>

        <button
          onClick={handleBulkSummarize}
          disabled={selected.size === 0 || anyBusy}
          title="Regenerate AI summaries for selected meetings"
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-brand-700/60 text-brand-300 bg-brand-600/10 hover:bg-brand-600/20 transition-colors disabled:opacity-40 disabled:hover:bg-brand-600/10"
        >
          {bulkBusy === "summarize" ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
          Summarize
        </button>
        <button
          onClick={handleBulkRecompute}
          disabled={selected.size === 0 || anyBusy}
          title="Recompute speaker voices for selected meetings"
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-gray-800 text-gray-300 bg-gray-900/60 hover:border-gray-700 hover:text-gray-100 transition-colors disabled:opacity-40 disabled:hover:border-gray-800 disabled:hover:text-gray-300"
        >
          {bulkBusy === "recompute" ? <Loader size={12} className="animate-spin" /> : <Speech size={12} />}
          Recompute
        </button>
        <button
          onClick={handleBulkDelete}
          disabled={selected.size === 0 || anyBusy}
          title="Delete selected meetings"
          className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-lg border border-red-800/50 text-red-400 bg-red-950/30 hover:bg-red-900/30 transition-colors disabled:opacity-40 disabled:hover:bg-red-950/30"
        >
          {bulkBusy === "delete" ? <Loader size={12} className="animate-spin" /> : <Trash2 size={12} />}
          Delete
        </button>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-5">
        {filtered.length === 0 && !loading && (
          <div className="text-gray-500 text-sm text-center py-12">
            {query ? "No transcriptions match." : "No meetings on this date."}
          </div>
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
              highlighted={selectedId === m.id || highlightId === m.id}
              processing={processingId === m.id}
              busy={cardBusy[m.id]}
              disabled={anyBusy}
              onToggleSelect={toggle}
              onOpen={onOpen}
              onAction={handleCardAction}
            />
          ))}
        </div>
      </div>

      {/* ── Delete confirmation modal ────────────────────────────────────────
          Same dialog UX as the sidebar's MeetingList — fixed-position
          backdrop, single Cancel/Delete button row. The destructive button
          stays enabled while the API request is in flight (showing
          "Deleting…") so the user can see progress; Cancel disables to
          prevent racing with the in-flight request. */}
      {confirmDelete !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl p-5 w-80">
            {confirmDelete.kind === "single" ? (
              <>
                <h3 className="text-sm font-semibold text-gray-100 mb-1">
                  Delete this meeting?
                </h3>
                <p className="text-xs text-gray-400 mb-4 leading-relaxed">
                  <span className="text-gray-200">"{confirmDelete.title}"</span>{" "}
                  will be removed, along with its audio recording and Obsidian
                  vault file. This cannot be undone.
                </p>
              </>
            ) : (
              <>
                <h3 className="text-sm font-semibold text-gray-100 mb-1">
                  Delete {confirmDelete.ids.length} meeting{confirmDelete.ids.length !== 1 ? "s" : ""}?
                </h3>
                <p className="text-xs text-gray-400 mb-4 leading-relaxed">
                  Their audio recordings and Obsidian vault files will be
                  removed too. This cannot be undone.
                </p>
              </>
            )}
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setConfirmDelete(null)}
                disabled={deleteInFlight}
                className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 rounded-lg hover:bg-gray-800 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  if (confirmDelete.kind === "single") {
                    void performSingleDelete(confirmDelete.id);
                  } else {
                    void performBulkDelete(confirmDelete.ids);
                  }
                }}
                disabled={deleteInFlight}
                className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg transition-colors"
              >
                {deleteInFlight ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Memoised so typing in the search box (which rebuilds `filtered`) doesn't
// re-render every card. Callbacks take the meeting id instead of closing
// over it — keeps the onToggleSelect/onOpen identities stable across renders.
const MeetingCard = memo(function MeetingCard({
  m, active, selected, highlighted, processing, busy, disabled,
  onToggleSelect, onOpen, onAction,
}: {
  m: Meeting; active: boolean; selected: boolean; highlighted: boolean;
  processing: boolean;
  busy: CardAction | undefined; disabled: boolean;
  onToggleSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onAction: (id: string, action: CardAction) => void;
}) {
  const speakers = useSpeakers(m);
  const takeaways = useTakeaways(m);
  // `processing` wins over `highlighted` visually — the user's current
  // focus during a bulk run is the sweep cursor, not the last-opened card.
  const cardStyle = processing
    ? "border-amber-500/70 bg-amber-950/20 shadow-lg shadow-amber-500/20 ring-1 ring-amber-500/40"
    : highlighted
    ? "border-brand-500/60 bg-brand-950/30 shadow-lg shadow-brand-500/10"
    : "border-gray-800 bg-gray-900/40 hover:border-gray-700 hover:bg-gray-900/70";
  return (
    <div
      className={`relative flex flex-col rounded-xl border p-3.5 transition-all cursor-pointer ${cardStyle}`}
      onClick={() => onOpen(m.id)}
    >
      {!active && (
        <button
          onClick={(e) => { e.stopPropagation(); onToggleSelect(m.id); }}
          className="absolute top-3 right-3 text-gray-500 hover:text-brand-400"
          title="Select"
        >
          {selected ? <CheckSquare size={13} className="text-brand-400" /> : <Square size={13} />}
        </button>
      )}

      <div className="flex items-center gap-1.5 pr-6">
        {m.status === "recording" && <div className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />}
        {m.status === "processing" && <Loader size={10} className="animate-spin text-amber-400" />}
        {m.status === "done" && <FileText size={10} className="text-gray-500" />}
        <h3 className="text-sm font-semibold text-gray-100 truncate">{m.title}</h3>
      </div>
      <div className="flex items-center gap-1 text-[11px] text-gray-500 mt-0.5">
        <Clock size={10} />
        {fmtClockTime(m.started_at)}{m.ended_at && ` – ${fmtClockTime(m.ended_at)}`}
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

      {!active && (
        <div className="mt-auto pt-3 flex items-center justify-end gap-0.5">
          <CardActionButton
            icon={busy === "summarize" ? "loader" : "sparkles"}
            title="Regenerate AI summary for this meeting"
            tone="brand"
            disabled={disabled}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onAction(m.id, "summarize");
            }}
          />
          <CardActionButton
            icon={busy === "recompute" ? "loader" : "speech"}
            title="Recompute voices for this meeting"
            tone="neutral"
            disabled={disabled}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onAction(m.id, "recompute");
            }}
          />
          <CardActionButton
            icon={busy === "delete" ? "loader" : "trash"}
            title="Delete this meeting"
            tone="danger"
            disabled={disabled}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onAction(m.id, "delete");
            }}
          />
        </div>
      )}
    </div>
  );
});

function CardActionButton({
  icon, title, tone, disabled, onClick,
}: {
  icon: "sparkles" | "speech" | "trash" | "loader";
  title: string;
  tone: "brand" | "neutral" | "danger";
  disabled: boolean;
  onClick: (e: React.MouseEvent) => void;
}) {
  const toneClass =
    tone === "brand"
      ? "text-gray-500 hover:text-brand-400"
      : tone === "danger"
      ? "text-gray-500 hover:text-red-400"
      : "text-gray-500 hover:text-gray-200";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`p-1.5 rounded hover:bg-gray-800/70 transition-colors disabled:opacity-40 disabled:pointer-events-none ${toneClass}`}
    >
      {icon === "loader" && <Loader size={12} className="animate-spin" />}
      {icon === "sparkles" && <Sparkles size={12} />}
      {icon === "speech" && <Speech size={12} />}
      {icon === "trash" && <Trash2 size={12} />}
    </button>
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

// ── Date helpers ────────────────────────────────────────────────────────────

function todayIso(): string {
  const d = new Date();
  const y = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${mm}-${dd}`;
}

function shiftIso(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(y, (m ?? 1) - 1, d ?? 1);
  dt.setDate(dt.getDate() + days);
  const yy = dt.getFullYear();
  const mm = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return `${yy}-${mm}-${dd}`;
}
