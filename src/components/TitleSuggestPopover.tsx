import { useEffect, useRef, useState } from "react";
import { Check, Loader, Sparkles, X, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { Meeting } from "../lib/api";

interface Props {
  meetingId: string;
  /** Called after the chosen title is successfully saved. Parent should
   *  refresh the meeting's display + the Meeting Library list. */
  onRenamed: (newTitle: string) => void;
  /** Fires as soon as the suggestion fetch returns. The server persists
   *  a fresh summary as a side effect, so the parent should swap its
   *  local Meeting for the one returned here (new summary + action items).
   *  Called even if the user never picks a suggestion. */
  onAnalyzed?: (meeting: Meeting) => void;
  /** Close without picking anything. */
  onClose: () => void;
  /** Pixel anchor — top-left corner of the popover. Keep this in viewport;
   *  caller is responsible for placement math. */
  anchor: { top: number; left: number };
}

/** AI-title suggestion popover.
 *
 *  Renders as a fixed-position floating card. On mount it kicks off a
 *  single fetch to /meetings/:id/suggest-title; the user clicks one of
 *  the returned candidates to apply it via the existing rename endpoint
 *  (which already handles the Obsidian vault file rename).
 *
 *  Click-outside + Escape both close the popover. Multiple fetches are
 *  avoided with a re-entry guard — "Try again" triggers an explicit
 *  refetch. */
export function TitleSuggestPopover({ meetingId, onRenamed, onAnalyzed, onClose, anchor }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [applyingIdx, setApplyingIdx] = useState<number | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  // StrictMode mounts effects twice in dev, which would double the LLM
  // call on open. This ref guards the initial fetch so only one request
  // leaves the client per meeting-id.
  const initialFetchedRef = useRef(false);

  const fetchSuggestions = async () => {
    setLoading(true);
    setError(null);
    setSuggestions([]);
    try {
      const { suggestions, meeting } = await api.meetings.suggestTitle(meetingId);
      setSuggestions(suggestions ?? []);
      // Propagate the server's refreshed Meeting so the parent sees
      // the new summary + action items even if the user closes this
      // popover without picking a title.
      if (meeting && onAnalyzed) onAnalyzed(meeting);
    } catch (e: any) {
      setError(e?.message ?? "Failed to generate suggestions");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (initialFetchedRef.current) return;
    initialFetchedRef.current = true;
    fetchSuggestions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId]);

  // Dismiss on Escape or outside click.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const onDown = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("keydown", onKey);
    // Delay the mousedown listener by a frame so the click that opened
    // this popover doesn't immediately close it.
    const t = window.setTimeout(
      () => window.addEventListener("mousedown", onDown),
      0,
    );
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
      window.clearTimeout(t);
    };
  }, [onClose]);

  const apply = async (idx: number) => {
    if (applyingIdx !== null) return;
    const chosen = suggestions[idx];
    if (!chosen) return;
    setApplyingIdx(idx);
    try {
      await api.meetings.rename(meetingId, chosen);
      onRenamed(chosen);
      onClose();
    } catch (e: any) {
      setError(e?.message ?? "Rename failed");
      setApplyingIdx(null);
    }
  };

  return (
    <div
      ref={panelRef}
      className="fixed z-[1000] w-80 rounded-xl border border-gray-700 bg-gray-900/95 backdrop-blur-sm shadow-2xl p-3"
      style={{ top: anchor.top, left: anchor.left }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="flex items-center gap-2 mb-2">
        <Sparkles size={13} className="text-brand-400" />
        <div className="text-xs font-semibold text-gray-200">Suggest a title</div>
        <div className="flex-1" />
        <button
          onClick={onClose}
          className="text-gray-500 hover:text-gray-200 transition-colors"
          title="Close"
        >
          <X size={14} />
        </button>
      </div>

      {loading && (
        <div className="flex items-center gap-2 py-4 text-xs text-gray-400">
          <Loader size={13} className="animate-spin" />
          Reading the transcript…
        </div>
      )}

      {!loading && error && (
        <div className="py-2 space-y-2">
          <p className="text-xs text-red-400 leading-relaxed break-words">{error}</p>
          <button
            onClick={fetchSuggestions}
            className="flex items-center gap-1.5 text-xs text-gray-300 hover:text-gray-100 px-2 py-1 rounded border border-gray-700 hover:border-gray-500 transition-colors"
          >
            <RefreshCw size={11} />
            Try again
          </button>
        </div>
      )}

      {!loading && !error && suggestions.length === 0 && (
        <p className="py-3 text-xs text-gray-500">No suggestions returned.</p>
      )}

      {!loading && !error && suggestions.length > 0 && (
        <>
          <ul className="space-y-1">
            {suggestions.map((s, i) => {
              const busy = applyingIdx === i;
              const othersBusy = applyingIdx !== null && applyingIdx !== i;
              return (
                <li key={i}>
                  <button
                    onClick={() => apply(i)}
                    disabled={applyingIdx !== null}
                    className={`group w-full flex items-start gap-2 text-left px-2.5 py-2 rounded-lg border transition-colors ${
                      busy
                        ? "border-brand-600 bg-brand-600/10 text-brand-300"
                        : othersBusy
                        ? "border-gray-800 text-gray-500 cursor-wait"
                        : "border-gray-800 hover:border-brand-700 hover:bg-brand-600/5 text-gray-200"
                    }`}
                  >
                    {busy
                      ? <Loader size={12} className="animate-spin mt-0.5 flex-shrink-0" />
                      : <Check size={12} className="opacity-0 group-hover:opacity-100 text-brand-400 mt-0.5 flex-shrink-0 transition-opacity" />}
                    <span className="text-xs leading-snug break-words">{s}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            onClick={fetchSuggestions}
            disabled={applyingIdx !== null}
            className="mt-2 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-gray-500 hover:text-gray-300 transition-colors disabled:opacity-40"
          >
            <RefreshCw size={10} />
            Try again
          </button>
        </>
      )}
    </div>
  );
}
