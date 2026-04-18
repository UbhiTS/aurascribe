import { useMemo, useState } from "react";
import { ArrowLeft, Clock, Loader, Pencil, Sparkles, CheckSquare, Square, FileText } from "lucide-react";
import { api } from "../lib/api";
import type { Meeting, Person } from "../lib/api";
import { TranscriptView } from "../components/TranscriptView";

interface Props {
  meeting: Meeting | null;
  meetingId: string | null;
  setMeeting: (m: Meeting | null) => void;
  enrolled: Person[];
  onEnrolledChanged: () => void;
  onBack: () => void;
  onMeetingChanged: () => void;  // bump refreshKey so library re-loads
  onOpenMeeting: (id: string) => void;  // navigate to another meeting (e.g. part 2 after split)
}

export function Review({
  meeting, meetingId, setMeeting, enrolled, onEnrolledChanged,
  onBack, onMeetingChanged, onOpenMeeting,
}: Props) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [summarizing, setSummarizing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [transcriptKey, setTranscriptKey] = useState(0);

  const selfSpeaker = enrolled.find((p) => p.name === "Me")?.name ?? "Me";
  const actionItems = useMemo(() => parseActionItems(meeting?.action_items ?? null), [meeting]);
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
              <button
                onClick={handleSummarize}
                disabled={summarizing || busy}
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 border-brand-700 text-brand-400 bg-brand-600/10 hover:bg-brand-600/20"
              >
                {summarizing ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
                AI Summary
              </button>
            )}
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
                enrolled={enrolled}
                onEnrolledChanged={onEnrolledChanged}
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

function parseActionItems(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function fmtDuration(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60).toString().padStart(2, "0");
  const s = (total % 60).toString().padStart(2, "0");
  return h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
}
