import { useEffect, useRef, useState } from "react";
import { Check, X, Plus } from "lucide-react";
import { api } from "../lib/api";
import type { Person, Utterance } from "../lib/api";

const SPEAKER_COLORS: Record<string, string> = {
  Me: "text-brand-400",
  Person1: "text-emerald-400",
  Person2: "text-amber-400",
  Person3: "text-pink-400",
  Person4: "text-cyan-400",
  Person5: "text-purple-400",
};

function getSpeakerColor(name: string): string {
  if (SPEAKER_COLORS[name]) return SPEAKER_COLORS[name];
  const colors = ["text-emerald-400", "text-amber-400", "text-pink-400", "text-cyan-400", "text-purple-400"];
  let hash = 0;
  for (const c of name) hash = (hash * 31 + c.charCodeAt(0)) & 0xffffffff;
  return colors[Math.abs(hash) % colors.length];
}

function fmtTime(s: number): string {
  const m = Math.floor(s / 60).toString().padStart(2, "0");
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

interface Props {
  meetingId: string | null;
  liveUtterances: Utterance[];
  livePartial: { speaker: string; text: string } | null;
  isRecording: boolean;
  enrolled?: Person[];
  onEnrolledChanged?: () => void;
}

interface RenameState {
  speaker: string;
  newName: string;
}

export function TranscriptView({
  meetingId,
  liveUtterances,
  livePartial,
  isRecording,
  enrolled = [],
  onEnrolledChanged,
}: Props) {
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [rename, setRename] = useState<RenameState | null>(null);
  // Per-utterance assign popover: keyed by utterance id.
  const [assignOpen, setAssignOpen] = useState<string | null>(null);
  const [newSpeakerDraft, setNewSpeakerDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!meetingId) { setUtterances([]); return; }
    api.meetings.get(meetingId).then((m) => setUtterances(m.utterances ?? []));
  }, [meetingId]);

  useEffect(() => {
    if (liveUtterances.length === 0) return;
    setUtterances((prev) => {
      const seen = new Set(prev.map((u) => u.id ?? `${u.start_time}-${u.speaker}`));
      const fresh = liveUtterances.filter((u) => !seen.has(u.id ?? `${u.start_time}-${u.speaker}`));
      return [...prev, ...fresh];
    });
  }, [liveUtterances]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [utterances, livePartial]);

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

  const handleBulkRename = async () => {
    if (!rename || !meetingId) return;
    await api.meetings.renameSpeaker(meetingId, rename.speaker, rename.newName);
    setUtterances((prev) =>
      prev.map((u) => u.speaker === rename.speaker ? { ...u, speaker: rename.newName } : u)
    );
    setRename(null);
    onEnrolledChanged?.();
  };

  const handleAssign = async (utteranceId: string, speaker: string) => {
    if (!meetingId) return;
    try {
      const res = await api.meetings.assignSpeaker(meetingId, utteranceId, speaker);
      setUtterances((prev) =>
        prev.map((u) => (u.id === utteranceId ? { ...u, speaker: res.speaker } : u))
      );
    } catch (e) {
      console.error("assign failed", e);
    }
    setAssignOpen(null);
    setNewSpeakerDraft("");
    onEnrolledChanged?.();
  };

  const speakers = [...new Set(utterances.map((u) => u.speaker))];

  if (!meetingId && !isRecording) {
    return (
      <div className="flex-1 flex items-center justify-center text-gray-600 text-sm">
        Start recording or select a meeting to view transcript
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Speaker legend + bulk rename */}
      {speakers.length > 0 && (
        <div className="flex flex-wrap gap-2 px-4 py-2 border-b border-gray-800">
          {speakers.map((s) => (
            <button
              key={s}
              onClick={() => setRename({ speaker: s, newName: s })}
              className={`text-xs px-2 py-0.5 rounded-full border border-gray-700 hover:border-gray-500 ${getSpeakerColor(s)} transition-colors`}
            >
              {s}
            </button>
          ))}
          <span className="text-xs text-gray-600 self-center">Click speaker to rename all</span>
        </div>
      )}

      {rename && (
        <div className="px-4 py-2 bg-gray-900 border-b border-gray-700 flex items-center gap-2">
          <span className="text-xs text-gray-400">Rename "{rename.speaker}" to:</span>
          <input
            autoFocus
            value={rename.newName}
            onChange={(e) => setRename({ ...rename, newName: e.target.value })}
            onKeyDown={(e) => { if (e.key === "Enter") handleBulkRename(); if (e.key === "Escape") setRename(null); }}
            className="text-xs bg-gray-800 border border-gray-600 rounded px-2 py-1 outline-none text-gray-200"
          />
          <button onClick={handleBulkRename} className="text-emerald-400 hover:text-emerald-300"><Check size={14} /></button>
          <button onClick={() => setRename(null)} className="text-gray-500 hover:text-gray-400"><X size={14} /></button>
        </div>
      )}

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-4 py-4 space-y-3">
        {utterances.map((u, i) => {
          const popoverOpen = u.id !== undefined && assignOpen === u.id;
          return (
            <div key={u.id ?? i} className="flex gap-3 group">
              <span className="text-xs text-gray-600 font-mono w-10 flex-shrink-0 pt-0.5">
                {fmtTime(u.start_time)}
              </span>
              <div className="flex-1 relative">
                <button
                  onClick={() => u.id !== undefined && setAssignOpen(popoverOpen ? null : u.id)}
                  disabled={u.id === undefined}
                  className={`text-xs font-semibold uppercase tracking-wider ${getSpeakerColor(u.speaker)} hover:underline decoration-dotted underline-offset-2 disabled:no-underline disabled:cursor-default`}
                  title={u.id !== undefined ? "Click to reassign this line" : undefined}
                >
                  {u.speaker}
                </button>
                <p className="text-sm text-gray-200 mt-0.5 leading-relaxed">{u.text}</p>

                {popoverOpen && u.id !== undefined && (
                  <div
                    data-assign-popover
                    className="absolute z-20 mt-1 bg-gray-900 border border-gray-700 rounded-lg shadow-xl min-w-[200px] p-1.5"
                  >
                    <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-gray-500">
                      Assign this line to
                    </div>
                    {enrolled.map((p) => (
                      <button
                        key={p.id}
                        onClick={() => handleAssign(u.id!, p.name)}
                        className={`w-full text-left px-2 py-1 text-sm rounded hover:bg-gray-800 ${getSpeakerColor(p.name)}`}
                      >
                        {p.name}
                      </button>
                    ))}
                    <button
                      onClick={() => handleAssign(u.id!, "Unknown")}
                      className="w-full text-left px-2 py-1 text-sm rounded text-gray-400 hover:bg-gray-800"
                    >
                      Unknown
                    </button>
                    <div className="border-t border-gray-800 mt-1 pt-1">
                      <div className="flex items-center gap-1 px-2">
                        <Plus size={12} className="text-gray-500" />
                        <input
                          value={newSpeakerDraft}
                          onChange={(e) => setNewSpeakerDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && newSpeakerDraft.trim()) {
                              handleAssign(u.id!, newSpeakerDraft.trim());
                            }
                          }}
                          placeholder="New speaker"
                          className="flex-1 text-sm bg-transparent outline-none text-gray-200 py-1"
                        />
                        {newSpeakerDraft.trim() && (
                          <button
                            onClick={() => handleAssign(u.id!, newSpeakerDraft.trim())}
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
          );
        })}

        {livePartial && isRecording && (
          <div className="flex gap-3 opacity-80">
            <span className="text-xs text-gray-600 font-mono w-10 flex-shrink-0 pt-0.5">live</span>
            <div className="flex-1 min-w-0">
              <span className={`text-xs font-semibold uppercase tracking-wider ${getSpeakerColor(livePartial.speaker)}`}>
                {livePartial.speaker}
              </span>
              <p className="text-sm text-gray-200 mt-0.5 leading-relaxed break-words">
                {livePartial.text}
                <span className="inline-block w-0.5 h-3.5 bg-gray-400 ml-0.5 animate-pulse align-middle" />
              </p>
            </div>
          </div>
        )}

        {isRecording && !livePartial && utterances.length === 0 && (
          <div className="flex items-center gap-2 text-gray-500 text-sm">
            <div className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            Listening...
          </div>
        )}

        <div ref={bottomRef} />
      </div>
    </div>
  );
}
