import { useEffect, useRef, useState } from "react";
import { Mic, MicOff, Square, Clock, ExternalLink, X } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { MicAudioProvider } from "../lib/MicAudioContext";
import { VuMeter } from "./VuMeter";
import { Waveform } from "./Waveform";

interface MicError {
  message: string;
  kind: "permission" | "unknown";
}

interface Props {
  isRecording: boolean;
  devices: { index: number; name: string }[];
  onStarted: (id: string) => void;
  onStopped: () => void;
}

export function RecordingBar({ isRecording, devices, onStarted, onStopped }: Props) {
  const [deviceIndex, setDeviceIndex] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [micError, setMicError] = useState<MicError | null>(null);
  const [openingSettings, setOpeningSettings] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const selectedDeviceName = devices.find((d) => d.index === deviceIndex)?.name ?? null;

  const startTimer = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
  };

  const stopTimer = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setElapsed(0);
  };

  // Guarantee the interval is cleared if the component unmounts mid-recording
  // (e.g. user navigates away). Without this, the setInterval would keep
  // firing setElapsed against an unmounted component.
  useEffect(() => () => {
    if (timerRef.current) clearInterval(timerRef.current);
  }, []);

  const handleStart = async () => {
    setLoading(true);
    setMicError(null);
    try {
      const res = await api.meetings.start("", deviceIndex);
      onStarted(res.meeting_id);
      startTimer();
    } catch (e) {
      // 403 from the sidecar carries structured {message, kind} detail —
      // "permission" means we know Windows is blocking us and can offer
      // the one-click settings opener. Any other mic-related failure gets
      // shown as a generic error with the same UI shape.
      if (e instanceof ApiError && e.status === 403) {
        const d = (e.detail ?? {}) as { message?: string; kind?: string };
        setMicError({
          message: d.message ?? e.message,
          kind: d.kind === "permission" ? "permission" : "unknown",
        });
      } else if (e instanceof Error) {
        setMicError({ message: e.message, kind: "unknown" });
      } else {
        setMicError({ message: "Could not start recording.", kind: "unknown" });
      }
    } finally {
      setLoading(false);
    }
  };

  const handleOpenMicSettings = async () => {
    setOpeningSettings(true);
    try {
      await api.system.openMicSettings();
    } catch {
      // Best-effort — if the shell dispatch fails, the modal stays open
      // with the instruction text, which is the fallback the user needs.
    } finally {
      setOpeningSettings(false);
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
    <MicAudioProvider deviceName={selectedDeviceName}>
    <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border transition-all ${
      isRecording ? "bg-red-950/40 border-red-800/50" : "bg-gray-900 border-gray-800"
    }`}>
      <div className={`flex-shrink-0 w-3 h-3 rounded-full ${
        isRecording ? "bg-red-500 animate-pulse" : "bg-gray-600"
      }`} />
      <VuMeter />
      <Waveform />

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
        <div className="ml-auto flex items-center gap-3">
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
        </div>
      )}
    </div>

    {micError && (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
        <div className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl p-5 w-96">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div className="flex items-center gap-2">
              <MicOff size={16} className="text-red-400" />
              <h3 className="text-sm font-semibold text-gray-100">
                {micError.kind === "permission" ? "Microphone blocked" : "Couldn't start recording"}
              </h3>
            </div>
            <button
              onClick={() => setMicError(null)}
              className="text-gray-500 hover:text-gray-300"
              title="Dismiss"
            >
              <X size={14} />
            </button>
          </div>
          <p className="text-xs text-gray-300 leading-relaxed mb-4 whitespace-pre-wrap">
            {micError.message}
          </p>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setMicError(null)}
              className="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200 rounded-lg hover:bg-gray-800"
            >
              Dismiss
            </button>
            {micError.kind === "permission" && (
              <button
                onClick={handleOpenMicSettings}
                disabled={openingSettings}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white rounded-lg"
              >
                <ExternalLink size={11} />
                Open Windows mic settings
              </button>
            )}
          </div>
        </div>
      </div>
    )}
    </MicAudioProvider>
  );
}

