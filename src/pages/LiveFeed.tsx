import { useEffect, useRef, useState } from "react";
import { Sparkles, Loader, Pencil, CheckSquare, Square, RefreshCw, Lightbulb } from "lucide-react";
import type { AppStatus, LiveIntel, Meeting, Utterance, Voice } from "../lib/api";
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
  liveIntel: LiveIntel;
  intelTick: number;
  voices: Voice[];
  onVoicesChanged: () => void;
  onMeetingStarted: (id: string) => void;
  onMeetingStopped: () => void;
  bumpRefreshKey: () => void;
}

export function LiveFeed({
  appStatus, meeting, setMeeting, meetingId,
  liveUtterances, livePartial, liveIntel, intelTick, voices, onVoicesChanged,
  onMeetingStarted, onMeetingStopped, bumpRefreshKey,
}: Props) {
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [summarizing, setSummarizing] = useState(false);
  const [refreshingIntel, setRefreshingIntel] = useState(false);

  const isRecording = appStatus?.is_recording ?? false;
  // Self speaker name — default "Me" unless a Voice has been tagged as such.
  const selfSpeaker = voices.find((v) => v.name === "Me")?.name ?? "Me";

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

  const handleRefreshIntel = async () => {
    if (!meetingId || refreshingIntel) return;
    setRefreshingIntel(true);
    try {
      await api.intel.refresh(meetingId);
    } catch (e) {
      console.warn("Intel refresh failed", e);
    } finally {
      setRefreshingIntel(false);
    }
  };

  const finalActionItems = parseActionItems(meeting?.action_items ?? null);

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
                    <h1 className="text-xl font-bold text-gray-100 tracking-tight truncate">
                      {meeting?.title ?? (isRecording ? "Recording..." : "Ready to AuraScribe!")}
                    </h1>
                    {meeting && (
                      <button
                        onClick={() => { setTitleDraft(meeting.title); setEditingTitle(true); }}
                        title="Rename transcription"
                        className="flex-shrink-0 text-gray-500 hover:text-gray-200 transition-colors"
                      >
                        <Pencil size={14} />
                      </button>
                    )}
                  </div>
                )}
                {meeting?.started_at && (
                  <p className="text-xs text-gray-500 truncate mt-0.5">
                    {new Date(meeting.started_at).toLocaleString()}
                  </p>
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
                voices={voices}
                onVoicesChanged={onVoicesChanged}
              />
            </div>
          </div>
        </section>

        {/* Live Intelligence */}
        <aside className="min-h-0 overflow-y-auto space-y-3">
          <div className="flex items-center justify-between px-1 pt-1">
            <h2 className="text-xl font-bold text-gray-100 tracking-tight">Live Intelligence</h2>
            {isRecording && meetingId && (
              <button
                onClick={handleRefreshIntel}
                disabled={refreshingIntel}
                title="Refresh now (skip the debounce timer)"
                className="text-gray-500 hover:text-brand-400 disabled:opacity-40 transition-colors"
              >
                <RefreshCw size={14} className={refreshingIntel ? "animate-spin" : ""} />
              </button>
            )}
          </div>

          <SupportIntelligenceCard text={liveIntel.supportIntelligence} tick={intelTick} />

          <Card title="Action Items — You">
            {liveIntel.actionItemsSelf.length === 0 && finalActionItems.length === 0 ? (
              <p className="text-xs text-gray-500 italic">Nothing yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {liveIntel.actionItemsSelf.map((item, i) => (
                  <ActionItem key={`live-${i}`} text={item} />
                ))}
                {finalActionItems.map((item, i) => (
                  <ActionItem key={`final-${i}`} text={item} />
                ))}
              </ul>
            )}
          </Card>

          {liveIntel.actionItemsOthers.length > 0 && (
            <Card title="Action Items — Others">
              <ul className="space-y-1.5">
                {liveIntel.actionItemsOthers.map((item, i) => (
                  <li key={i} className="text-xs flex items-start gap-2">
                    <span className="font-medium text-brand-300 flex-shrink-0">{item.speaker}:</span>
                    <span className="text-gray-300">{item.item}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <Card title="Real-Time Highlights" gradient>
            {liveIntel.highlights.length > 0 ? (
              <ul className="space-y-1.5">
                {liveIntel.highlights.map((h, i) => (
                  <li key={i} className="text-xs text-gray-200 leading-relaxed flex gap-2">
                    <span className="text-brand-400 select-none">•</span>
                    <span>{h}</span>
                  </li>
                ))}
              </ul>
            ) : meeting?.summary ? (
              <pre className="text-xs text-gray-300 whitespace-pre-wrap font-sans leading-relaxed">
                {extractHighlights(meeting.summary)}
              </pre>
            ) : (
              <p className="text-xs text-gray-500 italic">
                {isRecording
                  ? "Highlights appear here as the conversation progresses."
                  : "Start recording — highlights stream in every ~20s."}
              </p>
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

function Card({ title, children, gradient }: {
  title: string;
  children: React.ReactNode;
  gradient?: boolean;
}) {
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

function SupportIntelligenceCard({ text, tick }: { text: string; tick: number }) {
  // Brief flash on each WS push so the user notices the panel changed.
  const [flash, setFlash] = useState(false);
  const firstRender = useRef(true);
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return; }
    setFlash(true);
    const t = setTimeout(() => setFlash(false), 1200);
    return () => clearTimeout(t);
  }, [tick]);

  const bullets = parseBullets(text);
  return (
    <div className={`rounded-xl border p-3.5 transition-all ${
      flash
        ? "bg-gradient-to-br from-amber-900/50 to-amber-950/40 border-amber-500/60 shadow-lg shadow-amber-500/20"
        : "bg-gradient-to-br from-amber-950/30 to-gray-900/40 border-amber-800/40 shadow-md shadow-amber-500/5"
    }`}>
      <div className="flex items-center gap-1.5 mb-2">
        <Lightbulb size={11} className="text-amber-400" />
        <div className="text-[10px] uppercase tracking-wider text-amber-300 font-semibold">
          Ask Now
        </div>
      </div>
      {bullets.length > 0 ? (
        <ul className="space-y-1.5">
          {bullets.map((b, i) => (
            <li key={i} className="text-xs text-gray-200 leading-relaxed flex gap-2">
              <span className="text-amber-400 select-none">→</span>
              <span>{b}</span>
            </li>
          ))}
        </ul>
      ) : text ? (
        <p className="text-xs text-gray-300 whitespace-pre-wrap leading-relaxed">{text}</p>
      ) : (
        <p className="text-xs text-gray-500 italic">
          Talking-point nudges appear here based on where the conversation is heading.
        </p>
      )}
    </div>
  );
}

function parseBullets(text: string): string[] {
  if (!text) return [];
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.startsWith("- ") || l.startsWith("* "))
    .map((l) => l.slice(2).trim())
    .filter(Boolean);
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
