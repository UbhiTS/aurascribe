import { useCallback, useEffect, useRef, useState } from "react";
import { Sparkles, Loader, Pencil, CheckSquare, Square, RefreshCw, Lightbulb } from "lucide-react";
import type { AppStatus, LiveIntel, Meeting, Utterance, Voice } from "../lib/api";
import { api } from "../lib/api";
import { useClockTick } from "../lib/useClockTick";
import { RecordingBar } from "../components/RecordingBar";
import { TranscriptView } from "../components/TranscriptView";
import { TitleSuggestPopover } from "../components/TitleSuggestPopover";
import { Avatar } from "../components/Avatar";
import { avatarSrcFor, colorForSpeaker } from "../lib/speakerColors";

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
  // Anchor + visibility for the AI title-suggestion popover.
  const [titleSuggestAnchor, setTitleSuggestAnchor] = useState<{ top: number; left: number } | null>(null);
  const suggestBtnRef = useRef<HTMLButtonElement | null>(null);
  // Distinct speakers in this live meeting, reported by TranscriptView.
  // Drives the chip row in the title header so the user can see the cast
  // building up as the diarizer clusters voices.
  const [roster, setRoster] = useState<string[]>([]);
  const handleRoster = useCallback((names: string[]) => setRoster(names), []);

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

  const finalActionItems = meeting?.action_items ?? [];

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Recording bar */}
      <div className="px-5 py-3 border-b border-gray-800/60">
        <RecordingBar
          isRecording={isRecording}
          devices={appStatus?.audio_devices ?? []}
          outputDevices={appStatus?.audio_output_devices ?? []}
          onStarted={onMeetingStarted}
          onStopped={onMeetingStopped}
          platform={appStatus?.platform}
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
                    {meeting && (
                      <button
                        ref={suggestBtnRef}
                        onClick={() => {
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
                    )}
                  </div>
                )}
                {meeting?.started_at && (
                  <p className="text-xs text-gray-500 truncate mt-0.5">
                    {new Date(meeting.started_at).toLocaleString()}
                  </p>
                )}
              </div>

              {roster.length > 0 && (
                <div className="flex-shrink-0 flex items-center gap-1.5 flex-wrap justify-end max-w-[55%]">
                  {roster.map((name) => {
                    const c = colorForSpeaker(name, voices);
                    return (
                      <span
                        key={name}
                        className={`inline-flex items-center gap-1.5 pl-1 pr-2 py-0.5 rounded-full border ${c.border} bg-gray-900/60 text-[11px] text-gray-200`}
                        title={name}
                      >
                        <Avatar name={name} size="xs" gradient={c.avatar} src={avatarSrcFor(name, voices)} />
                        <span className={name === selfSpeaker ? "text-brand-400" : ""}>{name}</span>
                      </span>
                    );
                  })}
                </div>
              )}

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
                onRosterChange={handleRoster}
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

          <LiveIntelProgress
            intelTick={intelTick}
            isRecording={isRecording}
            manualRefreshing={refreshingIntel}
            meetingId={meetingId}
            utteranceCount={liveUtterances.length}
          />

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

      {titleSuggestAnchor && meetingId && (
        <TitleSuggestPopover
          meetingId={meetingId}
          anchor={titleSuggestAnchor}
          onClose={() => setTitleSuggestAnchor(null)}
          onAnalyzed={(refreshed) => {
            // Combined LLM call also refreshes the summary — swap in
            // the whole row so Intelligence cards update immediately.
            setMeeting(refreshed);
            bumpRefreshKey();
          }}
          onRenamed={(newTitle) => {
            setMeeting(meeting ? { ...meeting, title: newTitle } : null);
            bumpRefreshKey();
          }}
        />
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────

/** Visual pacing for the live-intelligence refresh loop.
 *
 *  The sidecar schedules intel LLM runs with a debounce:
 *    - each new utterance schedules a run at `now + 20s`, cancelling
 *      any pending one → active conversation keeps pushing the run
 *      further out
 *    - hard cap of 60s since the last completion; past that, the
 *      scheduled task is allowed to fire instead of being rearmed
 *  Mirrors `RT_HIGHLIGHTS_DEBOUNCE_SEC` and
 *  `RT_HIGHLIGHTS_MAX_INTERVAL_SEC` in `sidecar/aurascribe/llm/realtime.py`.
 *
 *  To stay faithful to that schedule, the frontend tracks two
 *  timestamps directly and recomputes phase on every render tick —
 *  no accumulated "elapsed" counter that can drift.
 *
 *    - `lastIntelAt`     — when the most recent payload arrived.
 *                          Sets the 60s max-interval deadline.
 *    - `lastUtteranceAt` — when the most recent finalised utterance
 *                          arrived. Sets the 20s debounce deadline.
 *
 *  Phases (all render the same DOM shape so height never changes):
 *    1. idle         — not recording, or no utterances since the last
 *                      refresh yet → empty track, "Intelligence idle".
 *    2. countdown    — there has been an utterance since the last
 *                      refresh; remaining = 20s − (now − lastUtterance).
 *                      Shrinking bar, "Next refresh Ns". Each new
 *                      utterance snaps the bar back to 20s, matching
 *                      the server-side debounce reset.
 *    3. waiting      — we've been past one full 20s window since the
 *                      last intel (so the bar has refilled at least
 *                      once), but still no refresh. The server is
 *                      holding because the debounce keeps resetting.
 *                      Amber pulse, "Waiting for a pause…".
 *    4. refreshing   — 60s cap tripped, OR the user clicked Refresh
 *                      now, OR the 20s debounce just finished. Brand
 *                      ping, "Refreshing intelligence…".
 */
function LiveIntelProgress({
  intelTick,
  isRecording,
  manualRefreshing,
  meetingId,
  utteranceCount,
}: {
  intelTick: number;
  isRecording: boolean;
  manualRefreshing: boolean;
  /** Resets the component state when the user starts a new recording
   *  — otherwise the bar picks up where it left off from the prior
   *  meeting, which is nonsensical. */
  meetingId: string | null;
  /** Length of `liveUtterances`. Used only as a change signal to
   *  stamp `lastUtteranceAt`; the actual value isn't read. */
  utteranceCount: number;
}) {
  // Fetched from /api/settings/config on mount. Defaults match the
  // bundled config (20s debounce, 60s max interval) so the bar is
  // usable even before the fetch resolves — or if it fails.
  const [cfg, setCfg] = useState<{ debounce: number; max: number }>({
    debounce: 20,
    max: 60,
  });
  useEffect(() => {
    let cancelled = false;
    api.settings.getConfig().then((c) => {
      if (cancelled) return;
      const d = c.settings?.rt_highlights_debounce_sec?.effective;
      const m = c.settings?.rt_highlights_max_interval_sec?.effective;
      setCfg({
        debounce: typeof d === "number" && d > 0 ? d : 20,
        max: typeof m === "number" && m > 0 ? m : 60,
      });
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);
  const DEBOUNCE_SEC = cfg.debounce;
  const MAX_SEC = cfg.max;

  const [lastIntelAt, setLastIntelAt] = useState<number | null>(null);
  const [lastUtteranceAt, setLastUtteranceAt] = useState<number | null>(null);
  // First utterance after the most recent intel (or since recording
  // started, if no intel has fired yet). Needed because the backend's
  // first-cycle path does NOT reset the debounce on subsequent
  // utterances — the very first utterance after an intel locks in
  // the scheduled fire-time for that cycle. Only once `last_run_ts`
  // is set server-side does the cap-bounded debounce reset kick in.
  const [firstUtteranceSinceIntel, setFirstUtteranceSinceIntel] =
    useState<number | null>(null);
  const [lastSeenTick, setLastSeenTick] = useState(intelTick);
  const [lastSeenUtteranceCount, setLastSeenUtteranceCount] = useState(utteranceCount);
  // 500ms render tick while recording. 500ms is fast enough that the
  // "Ns" readout never looks frozen, slow enough not to thrash React.
  // Pure w.r.t. the wall clock — each render reads a fresh Date.now().
  const clockTick = useClockTick(500, isRecording);

  // Reset everything when a new recording starts. Using meetingId is
  // safer than isRecording: isRecording toggles in the stop window
  // too, and we want to keep showing the post-stop state cleanly.
  useEffect(() => {
    setLastIntelAt(null);
    setLastUtteranceAt(null);
    setFirstUtteranceSinceIntel(null);
    setLastSeenTick(intelTick);
    setLastSeenUtteranceCount(utteranceCount);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetingId]);

  // Stamp lastIntelAt on every new intel payload.
  useEffect(() => {
    if (intelTick !== lastSeenTick) {
      setLastSeenTick(intelTick);
      setLastIntelAt(Date.now());
      // Fresh cycle — the next utterance will arm the next run.
      setLastUtteranceAt(null);
      setFirstUtteranceSinceIntel(null);
    }
  }, [intelTick, lastSeenTick]);

  // Stamp utterance timestamps whenever liveUtterances grows. Mirrors
  // the backend's debounce reset on each finalised utterance.
  useEffect(() => {
    if (utteranceCount !== lastSeenUtteranceCount) {
      setLastSeenUtteranceCount(utteranceCount);
      if (utteranceCount > 0) {
        const now = Date.now();
        setLastUtteranceAt(now);
        // Only stamp the first-since-intel marker on the very first
        // utterance of a new cycle — this is what anchors the
        // first-cycle schedule that subsequent utterances can't shift.
        setFirstUtteranceSinceIntel((prev) => (prev === null ? now : prev));
      }
    }
  }, [utteranceCount, lastSeenUtteranceCount]);

  // clockTick drives the re-render cadence — we read it into a void
  // reference so TypeScript doesn't flag it as unused.
  void clockTick;
  const now = Date.now();

  // ── Pause-countdown timer (the debounce) ────────────────────────────────
  // Anchor for the 20s debounce. On the first cycle (no intel yet)
  // the anchor is firstUtteranceSinceIntel and does NOT reset on
  // subsequent utterances — the backend doesn't move the pending
  // task in that case. On subsequent cycles the anchor is the last
  // utterance, which resets on every new one.
  const pauseAnchor =
    lastIntelAt === null ? firstUtteranceSinceIntel : lastUtteranceAt;
  const pauseRemaining =
    pauseAnchor === null
      ? null
      : Math.max(0, DEBOUNCE_SEC - (now - pauseAnchor) / 1000);

  // ── Max-wait timer (the 60s hard cap) ───────────────────────────────────
  // Only exists once at least one intel has completed — the backend
  // doesn't apply the cap during the first cycle.
  const maxRemaining =
    lastIntelAt === null
      ? null
      : Math.max(0, MAX_SEC - (now - lastIntelAt) / 1000);

  // ── Phase derivation ────────────────────────────────────────────────────
  // The backend fires when EITHER timer reaches 0. A manual refresh
  // bypasses both. Between refreshes we show both bars counting down.
  const eitherAtZero =
    (pauseRemaining !== null && pauseRemaining === 0) ||
    (maxRemaining !== null && maxRemaining === 0);
  const hasStarted = firstUtteranceSinceIntel !== null || lastUtteranceAt !== null;

  const phase: "idle" | "active" | "refreshing" = (() => {
    if (!isRecording) return "idle";
    if (manualRefreshing) return "refreshing";
    // If no utterance has arrived in this cycle, the backend won't
    // fire no matter how long we wait — so don't claim "refreshing"
    // just because the max timer happens to have crossed 0 during a
    // long silence.
    if (!hasStarted) return "idle";
    if (eitherAtZero) return "refreshing";
    return "active";
  })();

  // ── Bar geometry ────────────────────────────────────────────────────────
  // Two layered fills on the same 60s-scale track:
  //
  //   Brand fill  = MIN(pause, max) / MAX_SEC
  //       → the actual countdown. Always ends at the timer that will
  //         fire first. Edge bounces right when utterances reset
  //         the pause timer, and creeps left as the max timer ticks
  //         down.
  //
  //   Amber fill  = MAX(0, max − pause) / MAX_SEC
  //       → "extra slack" beyond the pause timer. Only visible
  //         while pause is dominant; shrinks to 0 when the max
  //         cap starts constraining (final ~20s of a continuous
  //         conversation). Starts where brand ends and extends to
  //         the maxRemaining position.
  //
  // Total filled width = maxRemaining/MAX_SEC, monotonic (never
  // jumps right). The brand/amber boundary is the only thing that
  // moves on utterances — the right edge of the filled region
  // keeps shrinking regardless of talking.
  const minRemaining =
    pauseRemaining === null && maxRemaining === null
      ? null
      : pauseRemaining === null
        ? maxRemaining
        : maxRemaining === null
          ? pauseRemaining
          : Math.min(pauseRemaining, maxRemaining);

  const brandFillPct =
    minRemaining === null
      ? 0
      : Math.max(0, Math.min(100, (minRemaining / MAX_SEC) * 100));

  const amberFillPct =
    pauseRemaining === null || maxRemaining === null
      ? 0
      : Math.max(0, Math.min(100, (Math.max(0, maxRemaining - pauseRemaining) / MAX_SEC) * 100));

  // Only two label states: the steady-state countdown and the
  // in-flight refresh. Everything else (first cycle, max-dominant,
  // not-yet-started) collapses into the same "Next refresh in"
  // label — the countdown numbers on the right and the bar
  // colouring already convey the nuance.
  const labelText =
    phase === "refreshing" ? "Refreshing intelligence…" : "Next refresh in";
  const labelColor =
    phase === "refreshing" ? "text-brand-300" : "text-gray-400";

  // Right-aligned text on the label row. During active countdown we
  // show BOTH timers so the user never has to parse the bar — format
  // is "pause / max" to mirror the bar's brand-then-amber layering.
  // If one is null (first cycle = no max), show "—" for that slot.
  const remainingText = (() => {
    if (phase !== "active") return "";
    const p = pauseRemaining !== null ? `${Math.ceil(pauseRemaining)}s` : "—";
    const m = maxRemaining !== null ? `${Math.ceil(maxRemaining)}s` : "—";
    return `${p} / ${m}`;
  })();

  return (
    <div className="px-1 -mt-0.5 py-1 space-y-1">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.12em] font-medium">
        <span className={labelColor}>{labelText}</span>
        <span className="tabular-nums text-gray-400">{remainingText}</span>
      </div>
      <div className="h-1.5 rounded-full bg-gray-800/80 overflow-hidden relative">
        {/* Refreshing state: one full-width pulsing fill. */}
        {phase === "refreshing" && (
          <div
            className="absolute left-0 top-0 h-full w-full bg-gradient-to-r from-brand-600/50 via-brand-400 to-brand-600/50 animate-pulse"
            style={{ animationDuration: "1.2s" }}
          />
        )}
        {/* Countdown state: two layered fills, amber behind + brand in front,
            both left-anchored. Amber is offset to start where brand ends
            (see `left`) so the two segments look continuous rather than
            overlapping. */}
        {phase !== "refreshing" && (
          <>
            <div
              className="absolute top-0 h-full bg-gradient-to-r from-amber-600/70 to-amber-400/90 transition-[left,width] duration-500 ease-linear"
              style={{
                left: `${brandFillPct}%`,
                width: `${amberFillPct}%`,
              }}
            />
            <div
              className="absolute left-0 top-0 h-full bg-gradient-to-r from-brand-500 via-purple-500 to-cyan-500 transition-[width] duration-500 ease-linear"
              style={{ width: `${brandFillPct}%` }}
            />
          </>
        )}
      </div>
    </div>
  );
}

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
