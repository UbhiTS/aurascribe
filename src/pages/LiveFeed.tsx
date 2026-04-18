import { useState } from "react";
import { Sparkles, Loader, Pencil, CheckSquare, Square } from "lucide-react";
import type { AppStatus, Meeting, Person, Utterance } from "../lib/api";
import { api } from "../lib/api";
import { RecordingBar } from "../components/RecordingBar";
import { TranscriptView } from "../components/TranscriptView";

interface Props {
  appStatus: AppStatus | null;
  // The live meeting — fully isolated from Meeting Library / Review state.
  meeting: Meeting | null;
  setMeeting: (m: Meeting | null) => void;
  meetingId: string | null;
  liveUtterances: Utterance[];
  livePartial: { speaker: string; text: string } | null;
  enrolled: Person[];
  onEnrolledChanged: () => void;
  onMeetingStarted: (id: string) => void;
  onMeetingStopped: () => void;
  bumpRefreshKey: () => void;
}

export function LiveFeed({
  appStatus, meeting, setMeeting, meetingId,
  liveUtterances, livePartial, enrolled, onEnrolledChanged,
  onMeetingStarted, onMeetingStopped, bumpRefreshKey,
}: Props) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [summarizing, setSummarizing] = useState(false);

  const isRecording = appStatus?.is_recording ?? false;
  // Self speaker name = whatever's enrolled with id matching — default "Me".
  const selfSpeaker = enrolled.find((p) => p.name === "Me")?.name ?? "Me";

  const handleRenameTitle = async () => {
    if (!meetingId || !titleDraft.trim()) { setEditingTitle(false); return; }
    await api.meetings.rename(meetingId, titleDraft.trim());
    setMeeting(meeting ? { ...meeting, title: titleDraft.trim() } : null);
    bumpRefreshKey();
    setEditingTitle(false);
  };

  const handleSummarize = async () => {
    if (!meetingId || summarizing) return;
    setSummarizing(true);
    try {
      const updated = await api.meetings.summarize(meetingId);
      setMeeting(updated);
    } finally {
      setSummarizing(false);
    }
  };

  const actionItems = parseActionItems(meeting?.action_items ?? null);

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Recording bar */}
      <div className="px-5 py-3 border-b border-gray-800/60">
        <RecordingBar
          isRecording={isRecording}
          devices={appStatus?.audio_devices ?? []}
          onStarted={onMeetingStarted}
          onStopped={onMeetingStopped}
        />
      </div>

      {/* Main 2-column: transcript + live intelligence */}
      <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,1fr)_360px] gap-4 p-4">
        {/* Transcript — circuit-pattern card with gradient glow border */}
        <section className="min-h-0 relative rounded-2xl overflow-hidden glow-border glow-shadow bg-gray-950">
          <div className="relative z-10 h-full flex flex-col">
            <div className="flex items-center gap-3 px-5 pt-4 pb-3">
              <div className="flex-1 min-w-0">
                <h1 className="text-xl font-bold text-gray-100 tracking-tight">Transcription</h1>
                {editingTitle ? (
                  <input
                    autoFocus
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") handleRenameTitle(); if (e.key === "Escape") setEditingTitle(false); }}
                    onBlur={handleRenameTitle}
                    className="mt-0.5 w-full text-xs bg-gray-800 border border-gray-600 rounded px-2 py-0.5 text-gray-200 outline-none"
                  />
                ) : (
                  <div className="flex items-center gap-1.5 group/title min-w-0 mt-0.5">
                    <p className="text-xs text-gray-500 truncate">
                      {meeting?.title ?? (isRecording ? "Recording..." : "No transcription selected")}
                      {meeting?.started_at && ` · ${new Date(meeting.started_at).toLocaleString()}`}
                    </p>
                    {meeting && (
                      <button
                        onClick={() => { setTitleDraft(meeting.title); setEditingTitle(true); }}
                        title="Rename transcription"
                        className="flex-shrink-0 opacity-0 group-hover/title:opacity-100 text-gray-600 hover:text-gray-300 transition-all"
                      >
                        <Pencil size={11} />
                      </button>
                    )}
                  </div>
                )}
              </div>

              {meeting && meeting.status === "done" && (
                <button
                  onClick={handleSummarize}
                  disabled={summarizing}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg border transition-colors disabled:opacity-50 border-brand-700 text-brand-400 bg-brand-600/10 hover:bg-brand-600/20"
                >
                  {summarizing ? <Loader size={12} className="animate-spin" /> : <Sparkles size={12} />}
                  AI Summary
                </button>
              )}
            </div>

            <div className="flex-1 min-h-0 bg-circuit">
              <TranscriptView
                meetingId={meetingId}
                liveUtterances={liveUtterances}
                livePartial={livePartial}
                isRecording={isRecording}
                selfSpeaker={selfSpeaker}
                enrolled={enrolled}
                onEnrolledChanged={onEnrolledChanged}
              />
            </div>
          </div>
        </section>

        {/* Live Intelligence */}
        <aside className="min-h-0 overflow-y-auto space-y-3">
          <h2 className="text-xl font-bold text-gray-100 tracking-tight px-1 pt-1">Live Intelligence</h2>
          <Card title="Real-Time Highlights" gradient>
            {meeting?.summary ? (
              <pre className="text-xs text-gray-300 whitespace-pre-wrap font-sans leading-relaxed">
                {extractHighlights(meeting.summary)}
              </pre>
            ) : (
              <p className="text-xs text-gray-500 italic">
                Highlights appear here after you click AI Summary on a finished meeting.
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

          {meeting?.vault_path && (
            <Card title="Obsidian">
              <p className="text-xs text-gray-400 break-all">{meeting.vault_path}</p>
            </Card>
          )}
        </aside>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────

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

function extractHighlights(summary: string): string {
  // Pull the ## Summary / ## Key Decisions sections for the highlights card.
  const lines = summary.split("\n");
  const keep: string[] = [];
  let mode: "summary" | "decisions" | null = null;
  for (const line of lines) {
    if (/^##\s+Summary/i.test(line)) { mode = "summary"; keep.push(line); continue; }
    if (/^##\s+Key Decisions/i.test(line)) { mode = "decisions"; keep.push(line); continue; }
    if (/^##\s+/.test(line)) { mode = null; continue; }
    if (mode) keep.push(line);
  }
  return keep.join("\n").trim() || summary.slice(0, 400);
}
