import { useMemo, useRef, useState } from "react";
import { ArrowLeft, Clock, Loader, Pencil, Sparkles, CheckSquare, Square, FileText, Trash2, Wand2 } from "lucide-react";
import { api, tagsPending } from "../lib/api";
import type { Meeting, Voice } from "../lib/api";
import { TranscriptView } from "../components/TranscriptView";
import { TitleSuggestPopover } from "../components/TitleSuggestPopover";

interface Props {
  meeting: Meeting | null;
  meetingId: string | null;
  setMeeting: (m: Meeting | null) => void;
  voices: Voice[];
  onVoicesChanged: () => void;
  onBack: () => void;
  onMeetingChanged: () => void;  // bump refreshKey so library re-loads
  onOpenMeeting: (id: string) => void;  // navigate to another meeting (e.g. part 2 after split)
}

export function Review({
  meeting, meetingId, setMeeting, voices, onVoicesChanged,
  onBack, onMeetingChanged, onOpenMeeting,
}: Props) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [summarizing, setSummarizing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [transcriptKey, setTranscriptKey] = useState(0);
  // Delete confirmation — we don't use window.confirm() because the
  // Tauri webview silently returns falsy for it.
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // Anchor + visibility for the AI title-suggestion popover. The anchor
  // is the sparkles button's bounding box so the popover lands right
  // underneath it regardless of where the title is on screen.
  const [titleSuggestAnchor, setTitleSuggestAnchor] = useState<{ top: number; left: number } | null>(null);
  const suggestBtnRef = useRef<HTMLButtonElement | null>(null);

  const selfSpeaker = voices.find((v) => v.name === "Me")?.name ?? "Me";
  const actionItems = meeting?.action_items ?? [];
  const duration = useMemo(() => {
    if (!meeting?.started_at || !meeting?.ended_at) return null;
    const ms = new Date(meeting.ended_at).getTime() - new Date(meeting.started_at).getTime();
    return ms > 0 ? ms : null;
  }, [meeting]);

  const handleRenameTitle = async () => {
    if (!meetingId || !titleDraft.trim()) { setEditingTitle(false); return; }
    await api.meetings.rename(meetingId, titleDraft.trim());
    setMeeting(meeting ? { ...meeting, title: titleDraft.trim() } : null);
    onMeetingChanged();
    setEditingTitle(false);
  };

  const handleSummarize = async () => {
    if (!meetingId || summarizing) return;
    setSummarizing(true);
    try {
      const updated = await api.meetings.summarize(meetingId);
      setMeeting(updated);
      onMeetingChanged();
    } finally {
      setSummarizing(false);
    }
  };

  const reloadMeeting = async () => {
    if (!meetingId) return;
    const fresh = await api.meetings.get(meetingId);
    setMeeting(fresh);
    setTranscriptKey((k) => k + 1);
    onMeetingChanged();
  };

  const handleTrim = async (opts: { before?: number; after?: number }) => {
    if (!meetingId || busy) return;
    setBusy(true);
    try {
      await api.meetings.trim(meetingId, opts);
      await reloadMeeting();
    } finally {
      setBusy(false);
    }
  };

  const handleSplit = async (at: number) => {
    if (!meetingId || busy) return;
    setBusy(true);
    try {
      const res = await api.meetings.split(meetingId, at);
      await reloadMeeting();
      onOpenMeeting(res.new_meeting_id);
    } finally {
      setBusy(false);
    }
  };

  const handleRecompute = async () => {
    if (!meetingId || recomputing) return;
    setRecomputing(true);
    try {
      await api.meetings.recompute(meetingId);
      await reloadMeeting();
    } catch (e: any) {
      alert(`Recompute failed: ${e.message ?? e}`);
    } finally {
      setRecomputing(false);
    }
  };

  const handleDelete = async () => {
    if (!meetingId || deleting) return;
    setDeleting(true);
    try {
      await api.meetings.delete(meetingId);
      // Bump the library's refresh key FIRST so it re-fetches without
      // this row, then navigate back. onBack alone doesn't trigger a
      // reload — the library's list is cached until refreshKey changes.
      onMeetingChanged();
      onBack();
    } catch (e: any) {
      setDeleting(false);
      setConfirmDelete(false);
      alert(`Delete failed: ${e.message ?? e}`);
    }
  };

  if (!meeting) {
    return (
      <div className="h-full flex items-center justify-center text-gray-500">
        <Loader size={16} className="animate-spin mr-2" />
        Loading meeting...
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Review bar — visually aligned with RecordingBar */}
      <div className="px-5 py-3 border-b border-gray-800/60">
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl border bg-gray-900 border-gray-800">
          <button
            onClick={onBack}
            title="Back to Meeting Library"
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-gray-300 bg-gray-800/60 border border-gray-700 hover:border-gray-600 hover:bg-gray-800 rounded-lg transition-colors"
          >
            <ArrowLeft size={13} />
            Library
          </button>

          <FileText size={13} className="text-gray-500 flex-shrink-0" />
          <span className="text-sm text-gray-300 font-medium">Reviewing</span>

          {duration !== null && (
            <span className="flex items-center gap-1 text-sm text-gray-400 font-mono">
              <Clock size={13} />
              {fmtDuration(duration)}
            </span>
          )}

          <div className="ml-auto flex items-center gap-2">
            {meeting.status === "done" && (
              <>
                {(() => {
                  const pending = tagsPending(meeting);
                  return (
                    <button
                      onClick={handleRecompute}
                      disabled={recomputing || busy || summarizing}
                      title={
                        pending
                          ? "Tags pending — Recompute to apply the latest Voices to this meeting's pills"
                          : "Re-run diarization against the current Voices DB — fixes mislabeled or merged speaker pills"
                      }
                      className={`flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 ${
                        pending
                          ? "border-amber-700 text-amber-300 bg-amber-950/30 hover:bg-amber-950/50"
                          : "border-gray-700 text-gray-300 bg-gray-800/60 hover:border-gray-500 hover:bg-gray-800"
                      }`}
                    >
                      {recomputing ? <Loader size={12} className="animate-spin" /> : <Wand2 size={12} />}
                      Recompute voices
                      {pending && !recomputing && (
                        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
                      )}
                    </button>
                  );
                })()}
                <button
                  onClick={handleSummarize}
                  disabled={summarizing || busy}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 border-brand-700 text-brand-400 bg-brand-600/10 hover:bg-brand-600/20"
                >
                  {summarizing ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
                  AI Summary
                </button>
              </>
            )}
            {/* Visual separator keeps the destructive delete from
                blurring with the positive actions on its left. */}
            <div className="h-5 w-px bg-gray-800 mx-1" />
            <button
              onClick={() => setConfirmDelete(true)}
              disabled={busy || summarizing || recomputing || deleting}
              title="Delete this meeting"
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 border-red-900/60 text-red-400 bg-red-950/20 hover:bg-red-950/40 hover:border-red-800"
            >
              <Trash2 size={12} />
              Delete
            </button>
          </div>
        </div>
      </div>

      {/* Main 2-column: transcript + intelligence */}
      <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,1fr)_360px] gap-4 p-4">
        <section className="min-h-0 relative rounded-2xl overflow-hidden glow-border glow-shadow bg-gray-950">
          <div className="relative z-10 h-full flex flex-col">
            <div className="flex items-center gap-3 px-5 pt-4 pb-3">
              <div className="flex-1 min-w-0">
                {editingTitle ? (
                  <input
                    autoFocus
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") handleRenameTitle(); if (e.key === "Escape") setEditingTitle(false); }}
                    onBlur={handleRenameTitle}
                    className="w-full text-xl font-bold bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-gray-100 tracking-tight outline-none focus:border-brand-500"
                  />
                ) : (
                  <div className="flex items-center gap-2 min-w-0">
                    <h1 className="text-xl font-bold text-gray-100 tracking-tight truncate">{meeting.title}</h1>
                    <button
                      onClick={() => { setTitleDraft(meeting.title); setEditingTitle(true); }}
                      title="Rename transcription"
                      className="flex-shrink-0 text-gray-500 hover:text-gray-200 transition-colors"
                    >
                      <Pencil size={14} />
                    </button>
                    <button
                      ref={suggestBtnRef}
                      onClick={() => {
                        // Anchor the popover to the button's bottom-left
                        // corner in viewport space.
                        const rect = suggestBtnRef.current?.getBoundingClientRect();
                        if (!rect) return;
                        setTitleSuggestAnchor({
                          top: rect.bottom + 6,
                          left: rect.left,
                        });
                      }}
                      title="Suggest a title with AI"
                      className="flex-shrink-0 text-gray-500 hover:text-brand-400 transition-colors"
                    >
                      <Sparkles size={14} />
                    </button>
                  </div>
                )}
                {meeting.started_at && (
                  <p className="text-xs text-gray-500 truncate mt-0.5">
                    {new Date(meeting.started_at).toLocaleString()}
                  </p>
                )}
              </div>
            </div>

            <div className="flex-1 min-h-0 bg-circuit">
              <TranscriptView
                meetingId={meetingId}
                liveUtterances={[]}
                livePartial={null}
                isRecording={false}
                selfSpeaker={selfSpeaker}
                voices={voices}
                onVoicesChanged={onVoicesChanged}
                editable
                onTrim={handleTrim}
                onSplit={handleSplit}
                refreshToken={transcriptKey}
              />
            </div>
          </div>
        </section>

        <aside className="min-h-0 overflow-y-auto space-y-3">
          <h2 className="text-xl font-bold text-gray-100 tracking-tight px-1 pt-1">Intelligence</h2>
          <Card title="Summary" gradient>
            {meeting.summary ? (
              <pre className="text-xs text-gray-300 whitespace-pre-wrap font-sans leading-relaxed">
                {meeting.summary}
              </pre>
            ) : (
              <p className="text-xs text-gray-500 italic">
                No summary yet — click AI Summary above.
              </p>
            )}
          </Card>

          <Card title="Action Items">
            {actionItems.length === 0 ? (
              <p className="text-xs text-gray-500 italic">No action items yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {actionItems.map((item, i) => (
                  <ActionItem key={i} text={item} />
                ))}
              </ul>
            )}
          </Card>

          {meeting.vault_path && (
            <Card title="Obsidian">
              <p className="text-xs text-gray-400 break-all">{meeting.vault_path}</p>
            </Card>
          )}
        </aside>
      </div>

      {titleSuggestAnchor && meetingId && (
        <TitleSuggestPopover
          meetingId={meetingId}
          anchor={titleSuggestAnchor}
          onClose={() => setTitleSuggestAnchor(null)}
          onAnalyzed={(refreshed) => {
            // The suggest-title endpoint also refreshes the summary as a
            // side effect. Swap in the whole row so Summary + Action
            // Items cards reflect it without a second fetch.
            setMeeting(refreshed);
            onMeetingChanged();
          }}
          onRenamed={(newTitle) => {
            setMeeting(meeting ? { ...meeting, title: newTitle } : null);
            onMeetingChanged();
          }}
        />
      )}

      {confirmDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl p-5 w-96">
            <h3 className="text-sm font-semibold text-gray-100 mb-1">
              Delete this meeting?
            </h3>
            <p className="text-xs text-gray-400 mb-4 leading-relaxed break-words">
              <span className="text-gray-300 font-medium">{meeting.title}</span>
              {" "}will be permanently removed from AuraScribe, along with its
              audio recording and Obsidian vault file. This cannot be undone.
            </p>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setConfirmDelete(false)}
                disabled={deleting}
                className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 rounded-lg hover:bg-gray-800 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg transition-colors"
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Card({ title, children, gradient }: { title: string; children: React.ReactNode; gradient?: boolean }) {
  return (
    <div className={`rounded-xl border p-3.5 ${
      gradient
        ? "bg-gradient-to-br from-brand-950/40 to-purple-950/40 border-brand-800/40 shadow-lg shadow-brand-500/5"
        : "bg-gray-900/60 border-gray-800"
    }`}>
      <div className="text-[10px] uppercase tracking-wider text-gray-400 font-semibold mb-2">{title}</div>
      {children}
    </div>
  );
}

function ActionItem({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <li className="flex items-start gap-2 text-xs">
      <button onClick={() => setDone(!done)} className="mt-0.5 text-gray-500 hover:text-brand-400 flex-shrink-0">
        {done ? <CheckSquare size={13} className="text-brand-400" /> : <Square size={13} />}
      </button>
      <span className={`text-gray-300 ${done ? "line-through text-gray-600" : ""}`}>{text}</span>
    </li>
  );
}

function fmtDuration(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60).toString().padStart(2, "0");
  const s = (total % 60).toString().padStart(2, "0");
  return h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
}
