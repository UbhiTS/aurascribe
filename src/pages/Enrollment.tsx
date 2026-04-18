import { useState } from "react";
import { Mic, Loader, CheckCircle2, RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { Person } from "../lib/api";
import { Avatar } from "../components/Avatar";

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
    title: "Natural speech",
    text:
      "Hi, I'm recording a voice sample so this app can recognize me. " +
      "Today is a good day to test this feature, and I'm speaking clearly and naturally.",
    seconds: 12,
  },
];

interface Props {
  enrolled: Person[];
  onEnrolledChanged: () => void;
}

export function Enrollment({ enrolled, onEnrolledChanged }: Props) {
  const [name, setName] = useState("Me");
  const [passageIdx, setPassageIdx] = useState(0);
  const passage = PASSAGES[passageIdx];
  const [status, setStatus] = useState<"idle" | "recording" | "done" | "error">("idle");
  const [message, setMessage] = useState("");
  const nameClash = enrolled.some((p) => p.name.toLowerCase() === name.trim().toLowerCase());

  const cycle = () => setPassageIdx((p) => (p + 1) % PASSAGES.length);

  const handleRecord = async () => {
    if (!name.trim()) return;
    setStatus("recording");
    setMessage(`Recording ${passage.seconds}s voice sample for "${name}"... Read the passage now.`);
    try {
      await api.enroll.start(name.trim(), passage.seconds);
      setStatus("done");
      setMessage(`Voice profile saved for "${name}".`);
      onEnrolledChanged();
    } catch (e: any) {
      setStatus("error");
      setMessage(e.message);
    }
  };

  // Pool count per name isn't exposed via /api/people (which returns one row per person).
  // So "samples" here shows 1+ if enrolled, with a "+ record again to improve" prompt.
  return (
    <div className="h-full overflow-y-auto scrollbar-thin">
      <div className="max-w-6xl mx-auto px-6 py-6">
        <h1 className="text-lg font-semibold text-gray-100">Voice Enrollment</h1>
        <p className="text-sm text-gray-400 mt-1">
          Train the speaker-identifier by reading a short passage. Re-enrolling an existing name adds another
          sample to their profile — more samples = better matching.
        </p>

        <div className="mt-6 grid grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)] gap-5">
          {/* Left: enrolled list */}
          <aside className="rounded-xl border border-gray-800 bg-gray-900/40 p-3">
            <div className="text-[10px] uppercase tracking-wider text-gray-400 font-semibold px-1 pb-2">
              Enrolled voices ({enrolled.length})
            </div>
            {enrolled.length === 0 ? (
              <div className="text-xs text-gray-500 italic px-1 py-4">No voices enrolled yet. Start by enrolling yourself as "Me".</div>
            ) : (
              <div className="space-y-1">
                {enrolled.map((p) => (
                  <div
                    key={p.id}
                    className="flex items-center gap-2.5 px-2 py-2 rounded-lg hover:bg-gray-900/70"
                  >
                    <Avatar name={p.name} size="sm" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-gray-200 truncate">{p.name}</div>
                      <div className="text-[10px] text-emerald-400 flex items-center gap-1">
                        <CheckCircle2 size={9} />
                        Enrolled
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </aside>

          {/* Right: record card */}
          <section className="rounded-xl border border-brand-800/40 bg-gradient-to-br from-brand-950/30 to-purple-950/20 p-5 shadow-lg shadow-brand-500/5">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold text-gray-100">Enroll new voice</h2>
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">
                {passage.title} · ~{passage.seconds}s
              </div>
            </div>

            <div>
              <label className="text-xs text-gray-400 mb-1 block">Name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={status === "recording"}
                className="w-full bg-gray-900/70 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 outline-none focus:border-brand-500"
              />
              {nameClash && status === "idle" && (
                <p className="text-xs text-amber-400 mt-1">
                  "{name.trim()}" is already enrolled — re-recording adds another sample (makes the profile stronger).
                </p>
              )}
            </div>

            <div className="mt-4 rounded-lg border border-gray-700 bg-gray-950/60 p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] uppercase tracking-wider text-gray-500">Read aloud</span>
                <button
                  onClick={cycle}
                  disabled={status === "recording"}
                  className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-gray-200 disabled:opacity-40"
                >
                  <RefreshCw size={10} /> Try another passage
                </button>
              </div>
              <p className="text-sm text-gray-200 leading-relaxed">{passage.text}</p>
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

            <div className="mt-5 flex items-center gap-3">
              <button
                onClick={handleRecord}
                disabled={status === "recording" || !name.trim()}
                className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-lg text-white text-sm font-medium transition-all
                  bg-gradient-to-r from-brand-600 to-purple-600 hover:from-brand-500 hover:to-purple-500
                  shadow-lg shadow-brand-500/30 hover:shadow-brand-500/50
                  disabled:opacity-50 disabled:shadow-none"
              >
                {status === "recording"
                  ? <><Loader size={15} className="animate-spin" /> Recording {passage.seconds}s...</>
                  : <><Mic size={15} /> Start recording</>
                }
              </button>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
