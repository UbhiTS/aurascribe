import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check, Pencil, Plus, Scissors, GitBranch,
  ArrowUpToLine, ArrowDownToLine, Play, Pause,
} from "lucide-react";
import { api } from "../lib/api";
import type { Person, Utterance } from "../lib/api";
import { Avatar } from "./Avatar";

// Distance threshold below which we consider a speaker match confident enough
// to visually merge into the previous bubble. Empirically, same-speaker
// matches sit in the 0.05–0.20 range; different speakers are 0.60+.
const MERGE_DIST_THRESHOLD = 0.20;
// Max gap (seconds) between adjacent utterances for a merge — beyond this
// it's probably a topic shift even if the voice is the same.
const MERGE_MAX_GAP_SEC = 3.0;

interface BubbleGroup {
  ids: string[];           // underlying utterance ids (retag targets)
  speaker: string;
  text: string;            // joined text of all members
  start_time: number;      // anchor — used for trim/split actions
  end_time: number;
  // Wall-clock seek target for this group's first utterance, + the speech-
  // time span that drives playback stop. Null when the meeting has no .opus.
  audio_start: number | null;
}

function groupUtterances(utterances: Utterance[]): BubbleGroup[] {
  const groups: BubbleGroup[] = [];
  for (const u of utterances) {
    const prev = groups[groups.length - 1];
    const prevLast = prev ? utterances.find((x) => x.id === prev.ids[prev.ids.length - 1]) : null;
    const canMerge =
      prev != null &&
      prevLast != null &&
      prev.speaker === u.speaker &&
      prev.speaker !== "Unknown" &&
      u.start_time - prevLast.end_time <= MERGE_MAX_GAP_SEC &&
      prevLast.match_distance != null &&
      u.match_distance != null &&
      prevLast.match_distance <= MERGE_DIST_THRESHOLD &&
      u.match_distance <= MERGE_DIST_THRESHOLD;
    if (canMerge) {
      if (u.id !== undefined) prev!.ids.push(u.id);
      prev!.text = `${prev!.text} ${u.text}`.trim();
      prev!.end_time = u.end_time;
    } else {
      groups.push({
        ids: u.id !== undefined ? [u.id] : [],
        speaker: u.speaker,
        text: u.text,
        start_time: u.start_time,
        end_time: u.end_time,
        audio_start: u.audio_start ?? null,
      });
    }
  }
  return groups;
}

function fmtTime(s: number): string {
  const m = Math.floor(s / 60).toString().padStart(2, "0");
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

// Deterministic bubble tint per non-self speaker, matches Avatar palette feel.
const BUBBLE_PALETTE = [
  "bg-emerald-500/15 border-emerald-500/30 text-emerald-50",
  "bg-cyan-500/15 border-cyan-500/30 text-cyan-50",
  "bg-amber-500/15 border-amber-500/30 text-amber-50",
  "bg-pink-500/15 border-pink-500/30 text-pink-50",
  "bg-purple-500/15 border-purple-500/30 text-purple-50",
  "bg-rose-500/15 border-rose-500/30 text-rose-50",
  "bg-teal-500/15 border-teal-500/30 text-teal-50",
];
function bubbleClassFor(name: string): string {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) & 0xffffffff;
  return BUBBLE_PALETTE[Math.abs(h) % BUBBLE_PALETTE.length];
}

interface Props {
  meetingId: string | null;
  liveUtterances: Utterance[];
  livePartial: { speaker: string; text: string } | null;
  isRecording: boolean;
  selfSpeaker?: string;  // name that should render right-aligned (yours)
  enrolled?: Person[];
  onEnrolledChanged?: () => void;
  editable?: boolean;    // show trim/split tools on each bubble
  onTrim?: (opts: { before?: number; after?: number }) => Promise<void> | void;
  onSplit?: (at: number) => Promise<void> | void;
  refreshToken?: number; // bump to force re-fetch after trim/split-style edits
}

export function TranscriptView({
  meetingId, liveUtterances, livePartial, isRecording,
  selfSpeaker = "Me", enrolled = [], onEnrolledChanged,
  editable = false, onTrim, onSplit, refreshToken = 0,
}: Props) {
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [assignOpen, setAssignOpen] = useState<string | null>(null);
  const [toolsOpen, setToolsOpen] = useState<string | null>(null);
  const [newSpeakerDraft, setNewSpeakerDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  // Shared <audio> element + which bubble is currently playing. We drive the
  // stop condition off a ref (not state) so the timeupdate handler stays
  // free of re-renders.
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const stopAtRef = useRef<number | null>(null);
  const [playingAnchorId, setPlayingAnchorId] = useState<string | null>(null);

  const stopPlayback = useCallback(() => {
    const el = audioRef.current;
    if (el && !el.paused) el.pause();
    stopAtRef.current = null;
    setPlayingAnchorId(null);
  }, []);

  const playSegment = useCallback(
    async (anchorId: string, audioStart: number, durationSec: number) => {
      if (!meetingId) return;
      const el = audioRef.current;
      if (!el) return;

      // Toggle: clicking the same bubble pauses.
      if (playingAnchorId === anchorId && !el.paused) {
        stopPlayback();
        return;
      }

      // Point the element at this meeting's audio — reassigning `.src`
      // between meetings triggers a fresh load, but same-meeting re-plays
      // are essentially free because the browser's HTTP cache (backed by
      // Range requests on our FileResponse) keeps the bytes warm.
      const desired = api.meetings.audioUrl(meetingId);
      if (!el.src.endsWith(desired)) el.src = desired;

      stopAtRef.current = audioStart + Math.max(0.2, durationSec);
      setPlayingAnchorId(anchorId);
      try {
        el.currentTime = audioStart;
        await el.play();
      } catch (e) {
        // No audio file (404), codec trouble, autoplay block — surface to
        // the console; silently fail in the UI so the pill just doesn't
        // animate.
        console.warn("audio playback failed", e);
        stopPlayback();
      }
    },
    [meetingId, playingAnchorId, stopPlayback],
  );

  // Stop at segment end; clear playing state on pause/end.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onTime = () => {
      const stopAt = stopAtRef.current;
      if (stopAt != null && el.currentTime >= stopAt) {
        el.pause();
      }
    };
    const onEnded = () => {
      stopAtRef.current = null;
      setPlayingAnchorId(null);
    };
    const onPause = () => {
      stopAtRef.current = null;
      setPlayingAnchorId(null);
    };
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("ended", onEnded);
    el.addEventListener("pause", onPause);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("ended", onEnded);
      el.removeEventListener("pause", onPause);
    };
  }, []);

  // Switching meetings stops any in-flight playback.
  useEffect(() => {
    stopPlayback();
  }, [meetingId, stopPlayback]);

  useEffect(() => {
    if (!meetingId) { setUtterances([]); return; }
    api.meetings.get(meetingId).then((m) => setUtterances(m.utterances ?? []));
  }, [meetingId, refreshToken]);

  useEffect(() => {
    if (liveUtterances.length === 0) return;
    setUtterances((prev) => {
      const seen = new Set(prev.map((u) => u.id ?? `${u.start_time}-${u.speaker}`));
      const fresh = liveUtterances.filter((u) => !seen.has(u.id ?? `${u.start_time}-${u.speaker}`));
      return [...prev, ...fresh];
    });
  }, [liveUtterances]);

  useEffect(() => {
    if (!isRecording) return;  // only auto-tail the live feed, not past meetings
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [utterances, livePartial, isRecording]);

  // Close popover on outside click / escape.
  useEffect(() => {
    if (assignOpen === null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setAssignOpen(null); };
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (!t.closest("[data-assign-popover]")) setAssignOpen(null);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [assignOpen]);

  useEffect(() => {
    if (toolsOpen === null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setToolsOpen(null); };
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement;
      if (!t.closest("[data-tools-popover]")) setToolsOpen(null);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [toolsOpen]);

  const handleTrimBefore = async (u: Utterance) => {
    if (!onTrim) return;
    if (!confirm(`Delete all lines before "${u.text.slice(0, 40)}${u.text.length > 40 ? "…" : ""}"?\n\nThis rebases remaining timestamps to 0.`)) return;
    await onTrim({ before: u.start_time });
    setToolsOpen(null);
  };

  const handleTrimAfter = async (u: Utterance) => {
    if (!onTrim) return;
    if (!confirm(`Delete all lines after "${u.text.slice(0, 40)}${u.text.length > 40 ? "…" : ""}"?`)) return;
    await onTrim({ after: u.start_time });
    setToolsOpen(null);
  };

  const handleSplitHere = async (u: Utterance) => {
    if (!onSplit) return;
    if (!confirm(`Split this meeting here?\n\nThis line and everything after becomes a new meeting (Part 2). Both meetings will need a fresh AI Summary.`)) return;
    await onSplit(u.start_time);
    setToolsOpen(null);
  };

  const handleAssign = async (utteranceIds: string[], oldSpeaker: string, speaker: string) => {
    if (!meetingId || utteranceIds.length === 0) return;
    // Provisional labels ("Speaker 1", "Speaker 2"...) always rename in bulk:
    // the user is telling us who the mystery speaker is, so every line tagged
    // with that number should flip — and the embeddings get folded into the
    // new person's enrollment so future chunks match automatically. Clearing
    // to "Unknown" stays per-utterance (single-line fix, not "forget them").
    const isProvisional = /^Speaker \d+$/.test(oldSpeaker);
    const isClear = !speaker || speaker.toLowerCase() === "unknown";
    const idSet = new Set(utteranceIds);
    try {
      if (isProvisional && !isClear) {
        await api.meetings.renameSpeaker(meetingId, oldSpeaker, speaker);
        setUtterances((prev) =>
          prev.map((u) => (u.speaker === oldSpeaker ? { ...u, speaker } : u))
        );
      } else if (isClear) {
        // Clearing: only the anchor utterance — keep the "lose the ability to
        // re-learn from one mistagged line" behavior scoped narrowly.
        const anchor = utteranceIds[0];
        const res = await api.meetings.assignSpeaker(meetingId, anchor, speaker);
        setUtterances((prev) =>
          prev.map((u) => (u.id === anchor ? { ...u, speaker: res.speaker } : u))
        );
      } else {
        // Enrolled assignment on a merged bubble — apply to every underlying
        // utterance so the whole pill flips (and every embedding folds in).
        for (const id of utteranceIds) {
          await api.meetings.assignSpeaker(meetingId, id, speaker);
        }
        setUtterances((prev) =>
          prev.map((u) => (u.id && idSet.has(u.id) ? { ...u, speaker } : u))
        );
      }
    } catch (e) {
      console.error("assign failed", e);
    }
    setAssignOpen(null);
    setNewSpeakerDraft("");
    onEnrolledChanged?.();
  };

  // Hooks must run unconditionally; keep this above any early returns.
  const groups = useMemo(() => groupUtterances(utterances), [utterances]);

  if (!meetingId && !isRecording) {
    return (
      <div className="h-full flex items-center justify-center text-gray-600 text-sm">
        Start recording or select a meeting to view transcript
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Shared audio element — one per TranscriptView instance. Never
          rendered visibly; control happens via playSegment / stopPlayback. */}
      <audio ref={audioRef} preload="none" style={{ display: "none" }} />
      <div className="flex-1 overflow-y-auto scrollbar-thin px-6 py-6 space-y-5">
        {groups.map((g, i) => {
          // Synthesize an Utterance-shaped object so the existing Bubble
          // renderer stays per-"bubble" — the DB rows below are still
          // individually addressable via `g.ids` for retag operations.
          const anchor: Utterance = {
            id: g.ids[0],
            speaker: g.speaker,
            text: g.text,
            start_time: g.start_time,
            end_time: g.end_time,
            audio_start: g.audio_start,
          };
          const anchorId = g.ids[0];
          const canPlay = anchorId !== undefined && g.audio_start != null;
          const isPlaying = canPlay && playingAnchorId === anchorId;
          return (
            <Bubble
              key={anchorId ?? i}
              u={anchor}
              mine={g.speaker === selfSpeaker}
              enrolled={enrolled}
              selfSpeaker={selfSpeaker}
              assignOpen={anchorId !== undefined && assignOpen === anchorId}
              onOpenAssign={() =>
                anchorId !== undefined && setAssignOpen(assignOpen === anchorId ? null : anchorId)
              }
              onAssign={(speaker) => handleAssign(g.ids, g.speaker, speaker)}
              newSpeakerDraft={newSpeakerDraft}
              onNewSpeakerDraft={setNewSpeakerDraft}
              editable={editable}
              toolsOpen={anchorId !== undefined && toolsOpen === anchorId}
              onOpenTools={() =>
                anchorId !== undefined && setToolsOpen(toolsOpen === anchorId ? null : anchorId)
              }
              onTrimBefore={() => handleTrimBefore(anchor)}
              onTrimAfter={() => handleTrimAfter(anchor)}
              onSplitHere={() => handleSplitHere(anchor)}
              isFirst={i === 0}
              isLast={i === groups.length - 1}
              canPlay={canPlay}
              isPlaying={isPlaying}
              onTogglePlay={() => {
                if (!canPlay || anchorId === undefined || g.audio_start == null) return;
                playSegment(anchorId, g.audio_start, g.end_time - g.start_time);
              }}
            />
          );
        })}

        {/* Live partial — grows as speech is recognised */}
        {livePartial && isRecording && (
          <LivePartialBubble
            speaker={livePartial.speaker}
            text={livePartial.text}
            mine={livePartial.speaker === selfSpeaker}
          />
        )}

        {isRecording && !livePartial && utterances.length === 0 && (
          <div className="flex items-center gap-2 text-gray-500 text-sm justify-center py-8">
            <div className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            Listening...
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}

// ── Bubble ──────────────────────────────────────────────────────────────────

interface BubbleProps {
  u: Utterance;
  mine: boolean;
  enrolled: Person[];
  selfSpeaker: string;
  assignOpen: boolean;
  onOpenAssign: () => void;
  onAssign: (speaker: string) => void;
  newSpeakerDraft: string;
  onNewSpeakerDraft: (v: string) => void;
  editable: boolean;
  toolsOpen: boolean;
  onOpenTools: () => void;
  onTrimBefore: () => void;
  onTrimAfter: () => void;
  onSplitHere: () => void;
  isFirst: boolean;
  isLast: boolean;
  canPlay: boolean;
  isPlaying: boolean;
  onTogglePlay: () => void;
}

function Bubble({
  u, mine, enrolled, selfSpeaker, assignOpen,
  onOpenAssign, onAssign, newSpeakerDraft, onNewSpeakerDraft,
  editable, toolsOpen, onOpenTools, onTrimBefore, onTrimAfter, onSplitHere,
  isFirst, isLast, canPlay, isPlaying, onTogglePlay,
}: BubbleProps) {
  return (
    <div className={`flex gap-3 items-start group/bubble ${mine ? "flex-row-reverse" : ""}`}>
      <Avatar name={u.speaker} size="md" />
      <div className={`flex-1 min-w-0 ${mine ? "items-end flex flex-col" : ""}`}>
        <div className={`flex items-center gap-2 mb-1 ${mine ? "flex-row-reverse" : ""}`}>
          <button
            onClick={onOpenAssign}
            disabled={u.id === undefined}
            className="group inline-flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border border-gray-700 hover:border-gray-500 text-gray-200 bg-gray-900/60 disabled:opacity-50 disabled:hover:border-gray-700"
          >
            {u.speaker}
            {u.id !== undefined && <Pencil size={9} className="opacity-40 group-hover:opacity-100 transition-opacity" />}
          </button>
          {canPlay ? (
            <button
              onClick={onTogglePlay}
              title={isPlaying ? "Stop playback" : "Play this segment"}
              className={`inline-flex items-center gap-1 text-[10px] font-mono rounded-full px-1.5 py-0.5 border transition-colors
                ${isPlaying
                  ? "border-brand-400/70 text-brand-300 bg-brand-500/10"
                  : "border-transparent text-gray-600 hover:text-gray-200 hover:border-gray-700"}`}
            >
              {isPlaying ? <Pause size={9} /> : <Play size={9} />}
              {fmtTime(u.start_time)}
            </button>
          ) : (
            <span className="text-[10px] font-mono text-gray-600">{fmtTime(u.start_time)}</span>
          )}
          {editable && u.id !== undefined && (
            <div className="relative">
              <button
                onClick={onOpenTools}
                title="Trim / Split"
                className="flex items-center justify-center w-5 h-5 rounded-full border border-gray-700 bg-gray-900/60 text-gray-500 hover:text-gray-200 hover:border-gray-500 opacity-0 group-hover/bubble:opacity-100 transition-all"
              >
                <Scissors size={10} />
              </button>
              {toolsOpen && (
                <div
                  data-tools-popover
                  className={`absolute z-30 top-full mt-2 bg-gray-900 border border-gray-700 rounded-lg shadow-xl min-w-[220px] p-1.5 text-gray-100 text-left
                    ${mine ? "right-0" : "left-0"}`}
                >
                  <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
                    Edit transcript
                  </div>
                  <button
                    onClick={onTrimBefore}
                    disabled={isFirst}
                    className="w-full text-left px-2 py-1.5 text-xs rounded hover:bg-gray-800 flex items-center gap-2 disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    <ArrowUpToLine size={12} className="text-gray-400" />
                    Trim before this line
                  </button>
                  <button
                    onClick={onTrimAfter}
                    disabled={isLast}
                    className="w-full text-left px-2 py-1.5 text-xs rounded hover:bg-gray-800 flex items-center gap-2 disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    <ArrowDownToLine size={12} className="text-gray-400" />
                    Trim after this line
                  </button>
                  <div className="border-t border-gray-800 my-1" />
                  <button
                    onClick={onSplitHere}
                    disabled={isFirst}
                    className="w-full text-left px-2 py-1.5 text-xs rounded hover:bg-gray-800 flex items-center gap-2 disabled:opacity-40 disabled:hover:bg-transparent"
                  >
                    <GitBranch size={12} className="text-amber-400" />
                    Split here (new meeting)
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        <div className={`relative inline-block max-w-[80%] px-3.5 py-2 rounded-2xl text-sm leading-relaxed border
          ${mine
            ? "bg-gradient-to-br from-brand-600 to-purple-700 text-white border-transparent shadow-lg shadow-brand-500/20 rounded-tr-sm"
            : `${bubbleClassFor(u.speaker)} rounded-tl-sm`}
          ${isPlaying ? "ring-2 ring-brand-400/60" : ""}`}
        >
          {u.text}

          {assignOpen && u.id !== undefined && (
            <div
              data-assign-popover
              className={`absolute z-30 top-full mt-2 bg-gray-900 border border-gray-700 rounded-lg shadow-xl min-w-[200px] p-1.5 text-gray-100 text-left
                ${mine ? "right-0" : "left-0"}`}
            >
              <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
                {/^Speaker \d+$/.test(u.speaker) ? `Tag all ${u.speaker} lines as` : "Assign this line to"}
              </div>
              {enrolled.length === 0 && (
                <div className="px-2 py-2 text-xs text-gray-500 italic">No enrolled speakers yet</div>
              )}
              {enrolled.map((p) => (
                <button
                  key={p.id}
                  onClick={() => onAssign(p.name)}
                  className="w-full text-left px-2 py-1 text-sm rounded hover:bg-gray-800 flex items-center gap-2"
                >
                  <Avatar name={p.name} size="xs" />
                  <span className={p.name === selfSpeaker ? "text-brand-400" : ""}>{p.name}</span>
                </button>
              ))}
              <button
                onClick={() => onAssign("Unknown")}
                className="w-full text-left px-2 py-1 text-sm rounded text-gray-400 hover:bg-gray-800"
              >
                Unknown
              </button>
              <div className="border-t border-gray-800 mt-1 pt-1">
                <div className="flex items-center gap-1 px-2">
                  <Plus size={12} className="text-gray-500" />
                  <input
                    value={newSpeakerDraft}
                    onChange={(e) => onNewSpeakerDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && newSpeakerDraft.trim()) onAssign(newSpeakerDraft.trim());
                    }}
                    placeholder="New speaker"
                    className="flex-1 text-sm bg-transparent outline-none text-gray-200 py-1"
                  />
                  {newSpeakerDraft.trim() && (
                    <button
                      onClick={() => onAssign(newSpeakerDraft.trim())}
                      className="text-emerald-400 hover:text-emerald-300"
                    >
                      <Check size={14} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function LivePartialBubble({ speaker, text, mine }: { speaker: string; text: string; mine: boolean }) {
  return (
    <div className={`flex gap-3 items-start opacity-75 ${mine ? "flex-row-reverse" : ""}`}>
      <Avatar name={speaker} size="md" />
      <div className={`flex-1 min-w-0 ${mine ? "items-end flex flex-col" : ""}`}>
        <div className={`flex items-center gap-2 mb-1 ${mine ? "flex-row-reverse" : ""}`}>
          <span className="text-[11px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border border-gray-700 text-gray-300 bg-gray-900/60">
            {speaker}
          </span>
          <span className="text-[10px] font-mono text-gray-600">live</span>
        </div>
        <div className={`inline-block max-w-[80%] px-3.5 py-2 rounded-2xl text-sm leading-relaxed break-words border border-dashed
          ${mine
            ? "bg-gradient-to-br from-brand-600/70 to-purple-700/70 text-white border-transparent rounded-tr-sm"
            : `${bubbleClassFor(speaker)} rounded-tl-sm`}`}
        >
          {text}
          <span className="inline-block w-0.5 h-3.5 bg-gray-300 ml-0.5 animate-pulse align-middle" />
        </div>
      </div>
    </div>
  );
}
