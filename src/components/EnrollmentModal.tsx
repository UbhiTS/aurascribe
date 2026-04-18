import { useState } from "react";
import { Mic, X, Loader, CheckCircle2, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { Person } from "../lib/api";

// Curated passages. Each pairs a typical spoken-word count with a sensible
// recording duration so the embedding has enough material to work with.
const PASSAGES: { title: string; text: string; seconds: number }[] = [
  {
    title: "Rainbow Passage",
    text:
      "When sunlight strikes raindrops in the air, they act like a prism and form a rainbow. " +
      "The rainbow is a division of white light into many beautiful colors.",
    seconds: 13,
  },
  {
    title: "Pangram set",
    text:
      "The quick brown fox jumps over the lazy dog. Pack my box with five dozen liquor jugs. " +
      "How vexingly quick daft zebras jump!",
    seconds: 11,
  },
  {
    title: "Numbers & names",
    text:
      "Hi, I'm recording a voice sample so this app can recognize me. " +
      "Today is a good day to test this feature, and I'm speaking clearly and naturally.",
    seconds: 12,
  },
];

interface Props {
  onClose: () => void;
  enrolled?: Person[];
}

export function EnrollmentModal({ onClose, enrolled = [] }: Props) {
  const [name, setName] = useState("Me");
  const [passageIdx, setPassageIdx] = useState(0);
  const passage = PASSAGES[passageIdx];
  const [duration, setDuration] = useState(passage.seconds);
  const [status, setStatus] = useState<"idle" | "recording" | "done" | "error">("idle");
  const [message, setMessage] = useState("");
  const nameClash = enrolled.some((p) => p.name.toLowerCase() === name.trim().toLowerCase());

  const cyclePassage = () => {
    const next = (passageIdx + 1) % PASSAGES.length;
    setPassageIdx(next);
    setDuration(PASSAGES[next].seconds);
  };

  const handleEnroll = async () => {
    if (!name.trim()) return;
    setStatus("recording");
    setMessage(`Recording ${duration}s voice sample for "${name}"... Speak normally.`);
    try {
      await api.enroll.start(name.trim(), duration);
      setStatus("done");
      setMessage(`Voice profile saved for "${name}". AuraScribe will now identify you automatically.`);
    } catch (e: any) {
      setStatus("error");
      setMessage(e.message);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
      <div className="bg-gray-900 border border-gray-700 rounded-2xl p-6 w-full max-w-md shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-gray-100">Voice Enrollment</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300">
            <X size={18} />
          </button>
        </div>

        <p className="text-sm text-gray-400 mb-3">
          Read the passage below naturally. AuraScribe will use it to learn your voice.
        </p>

        <div className="mb-4 rounded-lg border border-gray-700 bg-gray-950/60 p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] uppercase tracking-wider text-gray-500">
              {passage.title} · ~{passage.seconds}s
            </span>
            <button
              onClick={cyclePassage}
              disabled={status === "recording"}
              className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-gray-200 disabled:opacity-40"
              title="Try another passage"
            >
              <RefreshCw size={10} /> Try another
            </button>
          </div>
          <p className="text-sm text-gray-200 leading-relaxed">{passage.text}</p>
        </div>

        {enrolled.length > 0 && (
          <div className="mb-4 p-3 rounded-lg bg-emerald-950/30 border border-emerald-800/40">
            <div className="flex items-center gap-2 text-xs font-medium text-emerald-400 mb-1">
              <CheckCircle2 size={12} />
              Already enrolled
            </div>
            <div className="flex flex-wrap gap-1.5">
              {enrolled.map((p) => (
                <span
                  key={p.id}
                  className="px-2 py-0.5 text-xs rounded-full bg-emerald-900/50 text-emerald-200 border border-emerald-800/40"
                >
                  {p.name}
                </span>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-3">
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Your name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={status === "recording"}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
            />
            {nameClash && status === "idle" && (
              <p className="text-xs text-amber-400 mt-1">
                "{name.trim()}" is already enrolled — recording again will replace the existing voice profile.
              </p>
            )}
          </div>

          <div>
            <label className="text-xs text-gray-400 mb-1 block">Sample duration (seconds)</label>
            <input
              type="number"
              min={5}
              max={60}
              value={duration}
              onChange={(e) => setDuration(parseInt(e.target.value))}
              disabled={status === "recording"}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
            />
          </div>
        </div>

        {message && (
          <div className={`mt-4 p-3 rounded-lg text-sm ${
            status === "error" ? "bg-red-950/50 text-red-300 border border-red-800/50" :
            status === "done" ? "bg-emerald-950/50 text-emerald-300 border border-emerald-800/50" :
            "bg-amber-950/50 text-amber-300 border border-amber-800/50"
          }`}>
            {message}
          </div>
        )}

        <div className="flex gap-2 mt-5">
          {status !== "done" && (
            <button
              onClick={handleEnroll}
              disabled={status === "recording" || !name.trim()}
              className="flex-1 flex items-center justify-center gap-2 py-2 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
            >
              {status === "recording"
                ? <><Loader size={14} className="animate-spin" /> Recording...</>
                : <><Mic size={14} /> Start Recording</>
              }
            </button>
          )}
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
          >
            {status === "done" ? "Close" : "Cancel"}
          </button>
        </div>
      </div>
    </div>
  );
}
