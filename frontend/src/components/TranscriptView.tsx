import { useEffect, useRef, useState } from "react";
import { Check, X } from "lucide-react";
import { api } from "../lib/api";
import type { Utterance } from "../lib/api";

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
  // hash-based color for unknown speakers
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
  meetingId: number | null;
  liveUtterances: Utterance[];
  livePartial: { speaker: string; text: string } | null;
  isRecording: boolean;
}

interface RenameState {
  speaker: string;
  newName: string;
}

export function TranscriptView({ meetingId, liveUtterances, livePartial, isRecording }: Props) {
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [rename, setRename] = useState<RenameState | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Load historical utterances when switching meetings
  useEffect(() => {
    if (!meetingId) { setUtterances([]); return; }
    api.meetings.get(meetingId).then((m) => setUtterances(m.utterances ?? []));
  }, [meetingId]);

  // Append live utterances
  useEffect(() => {
    if (liveUtterances.length === 0) return;
    setUtterances((prev) => {
      const existing = new Set(prev.map((u) => `${u.start_time}-${u.speaker}`));
      const fresh = liveUtterances.filter((u) => !existing.has(`${u.start_time}-${u.speaker}`));
      return [...prev, ...fresh];
    });
  }, [liveUtterances]);

  // Auto-scroll on new utterances or while live partial is updating
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [utterances, livePartial]);

  const handleRename = async () => {
    if (!rename || !meetingId) return;
    await api.meetings.renameSpeaker(meetingId, rename.speaker, rename.newName);
    setUtterances((prev) =>
      prev.map((u) => u.speaker === rename.speaker ? { ...u, speaker: rename.newName } : u)
    );
    setRename(null);
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
      {/* Speaker legend + rename */}
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
          <span className="text-xs text-gray-600 self-center">Click speaker to rename</span>
        </div>
      )}

      {/* Rename modal */}
      {rename && (
        <div className="px-4 py-2 bg-gray-900 border-b border-gray-700 flex items-center gap-2">
          <span className="text-xs text-gray-400">Rename "{rename.speaker}" to:</span>
          <input
            autoFocus
            value={rename.newName}
            onChange={(e) => setRename({ ...rename, newName: e.target.value })}
            onKeyDown={(e) => { if (e.key === "Enter") handleRename(); if (e.key === "Escape") setRename(null); }}
            className="text-xs bg-gray-800 border border-gray-600 rounded px-2 py-1 outline-none text-gray-200"
          />
          <button onClick={handleRename} className="text-emerald-400 hover:text-emerald-300"><Check size={14} /></button>
          <button onClick={() => setRename(null)} className="text-gray-500 hover:text-gray-400"><X size={14} /></button>
        </div>
      )}

      {/* Transcript */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-4 py-4 space-y-3">
        {utterances.map((u, i) => (
          <div key={i} className="flex gap-3 group">
            <span className="text-xs text-gray-600 font-mono w-10 flex-shrink-0 pt-0.5">
              {fmtTime(u.start_time)}
            </span>
            <div className="flex-1">
              <span className={`text-xs font-semibold uppercase tracking-wider ${getSpeakerColor(u.speaker)}`}>
                {u.speaker}
              </span>
              <p className="text-sm text-gray-200 mt-0.5 leading-relaxed">{u.text}</p>
            </div>
          </div>
        ))}

        {/* Live partial — grows as speech is recognised, replaced by final on silence */}
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
