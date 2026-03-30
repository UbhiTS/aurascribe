import { useState } from "react";
import { Mic, X, Loader } from "lucide-react";
import { api } from "../lib/api";

interface Props {
  onClose: () => void;
}

export function EnrollmentModal({ onClose }: Props) {
  const [name, setName] = useState("Me");
  const [duration, setDuration] = useState(10);
  const [status, setStatus] = useState<"idle" | "recording" | "done" | "error">("idle");
  const [message, setMessage] = useState("");

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

        <p className="text-sm text-gray-400 mb-4">
          Record a voice sample so AuraScribe can identify who you are in recordings. Speak for {duration} seconds naturally — read something aloud or just talk.
        </p>

        <div className="space-y-3">
          <div>
            <label className="text-xs text-gray-400 mb-1 block">Your name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={status === "recording"}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
            />
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
