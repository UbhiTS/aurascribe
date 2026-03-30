import { useState } from "react";
import { Mic, Square, Clock } from "lucide-react";
import { api } from "../lib/api";
import { VuMeter } from "./VuMeter";

interface Props {
  isRecording: boolean;
  devices: { index: number; name: string }[];
  onStarted: (id: number) => void;
  onStopped: () => void;
}

export function RecordingBar({ isRecording, devices, onStarted, onStopped }: Props) {
  const [deviceIndex, setDeviceIndex] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useState<ReturnType<typeof setInterval> | null>(null);

  const startTimer = () => {
    if (timerRef[0]) clearInterval(timerRef[0]);
    const t = setInterval(() => setElapsed((e) => e + 1), 1000);
    (timerRef as any)[0] = t;
  };

  const stopTimer = () => {
    if (timerRef[0]) clearInterval(timerRef[0]);
    setElapsed(0);
  };

  const handleStart = async () => {
    setLoading(true);
    try {
      const res = await api.meetings.start("", deviceIndex);
      onStarted(res.meeting_id);
      startTimer();
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    setLoading(true);
    try {
      await api.meetings.stop(false);
      stopTimer();
      onStopped();
    } finally {
      setLoading(false);
    }
  };

  const fmt = (s: number) => {
    const m = Math.floor(s / 60).toString().padStart(2, "0");
    const sec = (s % 60).toString().padStart(2, "0");
    return `${m}:${sec}`;
  };

  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border transition-all ${
      isRecording ? "bg-red-950/40 border-red-800/50" : "bg-gray-900 border-gray-800"
    }`}>
      <div className={`flex-shrink-0 w-3 h-3 rounded-full ${
        isRecording ? "bg-red-500 animate-pulse" : "bg-gray-600"
      }`} />
      <VuMeter />

      {isRecording ? (
        <>
          <span className="text-sm text-gray-300 font-medium">Recording</span>
          <span className="flex items-center gap-1 text-sm text-red-400 font-mono">
            <Clock size={13} />
            {fmt(elapsed)}
          </span>
          <div className="ml-auto">
            <button
              onClick={handleStop}
              disabled={loading}
              className="flex items-center gap-2 px-3 py-1.5 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
            >
              <Square size={14} />
              Stop
            </button>
          </div>
        </>
      ) : (
        <>
          {devices.length > 1 && (
            <select
              value={deviceIndex ?? ""}
              onChange={(e) => setDeviceIndex(e.target.value ? parseInt(e.target.value) : undefined)}
              className="text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded px-2 py-1 outline-none"
            >
              <option value="">Default mic</option>
              {devices.map((d) => (
                <option key={d.index} value={d.index}>{d.name}</option>
              ))}
            </select>
          )}
          <button
            onClick={handleStart}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-1.5 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
          >
            <Mic size={14} />
            Start Recording
          </button>
        </>
      )}
    </div>
  );
}

