import { useEffect, useRef, useState } from "react";
import { Mic, MicOff, Square, Clock, ExternalLink, X, Volume2, Hourglass } from "lucide-react";
import { api, ApiError } from "../lib/api";
import type { AutoCaptureState } from "../lib/api";
import { MicAudioProvider } from "../lib/MicAudioContext";
import { VuMeter } from "./VuMeter";
import { Waveform } from "./Waveform";

interface MicError {
  message: string;
  kind: "permission" | "unknown";
}

// Three source modes the user can pick. Encodes intent — what's being
// recorded — instead of making the user compose it from raw device
// pickers. Labels mirror the <option> text in the UI.
type SourceMode = "mic" | "system" | "mix";

interface Props {
  isRecording: boolean;
  devices: { index: number; name: string }[];
  outputDevices: { index: number; name: string }[];
  onStarted: (id: string) => void;
  onStopped: () => void;
  // sys.platform from the sidecar — "win32", "darwin", or "linux".
  // Drives OS-specific labels (e.g. mic permission settings button).
  platform?: string;
  // Drives the "auto-stop in Xs" countdown. Only present when the
  // active meeting was started by the auto-capture monitor — manual
  // recordings never auto-stop, so the chip stays hidden.
  autoCaptureState?: AutoCaptureState | null;
  // ISO timestamp of when the active meeting started. Used to derive
  // the elapsed-time chip so the timer survives tab navigation — the
  // component state resets on unmount, but started_at lives upstream
  // on the liveMeeting object in App.
  meetingStartedAt?: string | null;
}

// localStorage keys. Device selection is stored by device *name* (not
// index), because sounddevice / soundcard indices aren't stable across
// reboots or USB re-plugs. Names are resolved back to current indices at
// read time; a missing name falls back to the system default.
const LS_MODE = "aurascribe.source.mode";
const LS_MIC_NAME = "aurascribe.source.mic";
const LS_SPEAKER_NAME = "aurascribe.source.speaker";

function readMode(): SourceMode {
  const v = window.localStorage.getItem(LS_MODE);
  return v === "mic" || v === "system" || v === "mix" ? v : "mic";
}

export function RecordingBar({
  isRecording, devices, outputDevices, onStarted, onStopped, platform, autoCaptureState,
  meetingStartedAt,
}: Props) {
  const [mode, setMode] = useState<SourceMode>(() => readMode());
  const [deviceIndex, setDeviceIndex] = useState<number | undefined>(undefined);
  // Loopback / system-audio source. undefined = no system-audio capture
  // in this session. Populated via localStorage + the current output list.
  const [loopbackIndex, setLoopbackIndex] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [micError, setMicError] = useState<MicError | null>(null);
  const [openingSettings, setOpeningSettings] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const selectedDeviceName = devices.find((d) => d.index === deviceIndex)?.name ?? null;

  // Resolve the persisted mic/speaker names back to current indices whenever
  // the device lists arrive or change. Names missing from the list fall
  // through to `undefined` (= OS default), which is the safe default when
  // the user's last-picked device was unplugged between sessions.
  useEffect(() => {
    const wantedMic = window.localStorage.getItem(LS_MIC_NAME);
    if (!wantedMic) { setDeviceIndex(undefined); return; }
    const found = devices.find((d) => d.name === wantedMic);
    setDeviceIndex(found?.index);
  }, [devices]);

  useEffect(() => {
    // Speaker names include a "(default)" suffix when they match the OS
    // default; strip it before comparing so a default change between
    // sessions doesn't invalidate the stored selection.
    const normalize = (s: string) => s.replace(/\s*\(default\)\s*$/i, "");
    const wantedSpk = window.localStorage.getItem(LS_SPEAKER_NAME);
    if (wantedSpk) {
      const wanted = normalize(wantedSpk);
      const found = outputDevices.find((d) => normalize(d.name) === wanted);
      if (found) { setLoopbackIndex(found.index); return; }
    }
    // No stored selection (or the stored endpoint has disappeared) — fall
    // through to the OS default speaker, i.e. the one tagged "(default)"
    // by the backend enumeration. Unlike the mic picker there's no
    // "Default speaker" sentinel option (soundcard needs a concrete id),
    // so we resolve the default explicitly here.
    const def = outputDevices.find((d) => /\(default\)/i.test(d.name));
    setLoopbackIndex(def?.index);
  }, [outputDevices]);

  useEffect(() => {
    window.localStorage.setItem(LS_MODE, mode);
  }, [mode]);
  const persistMic = (idx: number | undefined) => {
    setDeviceIndex(idx);
    const name = devices.find((d) => d.index === idx)?.name ?? "";
    if (name) window.localStorage.setItem(LS_MIC_NAME, name);
    else window.localStorage.removeItem(LS_MIC_NAME);
  };
  const persistLoopback = (idx: number | undefined) => {
    setLoopbackIndex(idx);
    const name = outputDevices.find((d) => d.index === idx)?.name ?? "";
    if (name) window.localStorage.setItem(LS_SPEAKER_NAME, name);
    else window.localStorage.removeItem(LS_SPEAKER_NAME);
  };

  // Drive the timer from `meetingStartedAt` when available so it
  // survives navigating away and back to Live Feed — RecordingBar
  // unmounts on tab switch, but the started_at timestamp lives
  // upstream on App's liveMeeting. When it's not yet available (first
  // few frames after a click, before api.meetings.get resolves), tick
  // from 0 so the display isn't frozen.
  useEffect(() => {
    if (!isRecording) {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      setElapsed(0);
      return;
    }
    const parsed = meetingStartedAt ? Date.parse(meetingStartedAt) : NaN;
    const anchor = Number.isFinite(parsed) ? parsed : Date.now();
    const recompute = () => setElapsed(Math.max(0, Math.floor((Date.now() - anchor) / 1000)));
    recompute();
    timerRef.current = setInterval(recompute, 1000);
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [isRecording, meetingStartedAt]);

  // Pre-recording monitor. When idle + mode is anything other than
  // "mic" (where browser getUserMedia already drives the visualizers),
  // keep the sidecar's capture pipeline open so VU + waveform animate
  // with the exact signal the user would record on Start. The server
  // tears the monitor down automatically when a real meeting starts.
  //
  // Important: `outputDevices` comes from a polled /api/status array
  // and gets a fresh reference every poll even when the content is
  // identical. Depending on it directly makes the effect thrash
  // dozens of times per second — re-opening and re-closing audio
  // streams each time. So we mirror it into a ref that the effect
  // reads on-demand, and key the effect only on the concrete picker
  // state the user actually changes.
  const outputDevicesRef = useRef(outputDevices);
  useEffect(() => { outputDevicesRef.current = outputDevices; }, [outputDevices]);
  useEffect(() => {
    if (isRecording) return;        // real meeting is emitting audio_level
    if (mode === "mic") return;     // browser getUserMedia path
    const outs = outputDevicesRef.current;
    const defaultSpk = outs.find((d) => /\(default\)/i.test(d.name))?.index;
    const resolvedLoopback = loopbackIndex ?? defaultSpk;
    if (resolvedLoopback === undefined) return;  // no output device available
    api.meetings.monitorStart({
      device: mode === "system" ? undefined : deviceIndex,
      loopbackDevice: resolvedLoopback,
      captureMic: mode !== "system",
    }).catch(() => {
      // Monitor failures are non-fatal — a mic permission denial or
      // loopback glitch just means the VU goes dark until the user picks
      // a different source or hits Start Recording.
    });
    return () => {
      api.meetings.monitorStop().catch(() => {});
    };
  }, [isRecording, mode, deviceIndex, loopbackIndex]);

  const handleStart = async () => {
    setLoading(true);
    setMicError(null);
    try {
      // Derive the three-way mode into the backend's two booleans — keeps
      // the API shape simple but still supports system-only (capture_mic
      // off, loopback on), mic-only, and mix.
      const captureMic = mode !== "system";
      // "Default speaker" selection has no dedicated index (soundcard needs
      // a concrete id), so when the user leaves it on the sentinel we
      // resolve the current default by the "(default)" suffix baked into
      // the enumeration names.
      const resolvedLoopback =
        loopbackIndex ??
        outputDevices.find((d) => /\(default\)/i.test(d.name))?.index;
      const loopbackDevice = mode === "mic" ? undefined : resolvedLoopback;
      const micDevice = captureMic ? deviceIndex : undefined;
      const res = await api.meetings.start("", {
        device: micDevice,
        loopbackDevice,
        captureMic,
      });
      onStarted(res.meeting_id);
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

  // Auto-stop countdown. Only fires when the auto-capture monitor is
  // silence-counting against an auto-started recording (the backend
  // zeros silent_seconds for manual starts, so this naturally stays
  // null there). Gated by `countdown_after_silence_sec` so the Stop
  // button stays quiet during normal short pauses — once silence
  // exceeds the gate, the button morphs into a live countdown.
  const autoStopRemaining: number | null = (() => {
    if (!isRecording || !autoCaptureState) return null;
    if (autoCaptureState.state !== "recording") return null;
    const silent = autoCaptureState.silent_seconds ?? 0;
    const threshold = autoCaptureState.stop_silence_seconds ?? 0;
    const gate = autoCaptureState.countdown_after_silence_sec ?? 5;
    if (silent < gate || threshold <= 0) return null;
    return Math.max(0, Math.ceil(threshold - silent));
  })();
  const autoStopUrgent = autoStopRemaining !== null && autoStopRemaining <= 10;

  return (
    <MicAudioProvider deviceName={selectedDeviceName}>
    <div className={`flex flex-wrap items-center gap-y-2 gap-x-3 px-4 py-3 rounded-xl border transition-all min-h-[72px] ${
      isRecording ? "bg-red-950/40 border-red-800/50" : "bg-gray-900 border-gray-800"
    }`}>
      <div className={`flex-shrink-0 w-3 h-3 rounded-full ${
        isRecording ? "bg-red-500 animate-pulse" : "bg-gray-600"
      }`} />
      <VuMeter />
      <Waveform />

      {isRecording && (
        <span className="flex items-center gap-1 text-sm text-red-400 font-mono flex-shrink-0">
          <Clock size={13} />
          {fmt(elapsed)}
        </span>
      )}

      {/* Source controls — always rendered, disabled during recording so
          the user can see what they're capturing from without being
          tempted to hot-swap mid-meeting (capture-pipeline switches
          mid-record aren't supported and the locking would fight HMR).
          The whole row is `flex-wrap` so at narrow widths the Start /
          Stop button wraps to a second line rather than getting pushed
          off-screen — and the device dropdowns shrink before that
          happens so the common case stays on one line. */}
      <div className="ml-auto flex flex-wrap items-center gap-y-2 gap-x-3 min-w-0">
        <label className="flex items-center gap-1.5 text-xs text-gray-500 flex-shrink-0" title="What to record">
          <span className="text-gray-400 hidden sm:inline">Source:</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as SourceMode)}
            disabled={isRecording}
            className="text-xs text-gray-200 bg-gray-800 border border-gray-700 rounded px-2 py-1 outline-none disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <option value="mic">Microphone</option>
            {outputDevices.length > 0 && (
              <option value="system">System Audio</option>
            )}
            {outputDevices.length > 0 && (
              <option value="mix">Mix (Mic + System Audio)</option>
            )}
          </select>
        </label>

        {/* Device pickers. In "mix" mode both render stacked in a single
            column so they don't steal horizontal space from Start / Stop.
            Widths are responsive (`max-w` ceiling that shrinks at narrow
            breakpoints) so a narrow window doesn't push the Record
            button off-screen. */}
        <div className={`flex gap-1.5 min-w-0 ${mode === "mix" ? "flex-col" : "flex-row items-center gap-3"}`}>
          {mode !== "system" && devices.length > 1 && (
            <label className="flex items-center gap-1.5 text-xs text-gray-500 min-w-0" title="Microphone">
              <Mic size={12} className="text-gray-500 flex-shrink-0" />
              <select
                value={deviceIndex ?? ""}
                onChange={(e) => persistMic(e.target.value ? parseInt(e.target.value) : undefined)}
                disabled={isRecording}
                className="text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded px-2 py-1 outline-none min-w-0 w-full sm:w-[180px] lg:w-[260px] 2xl:w-[330px] disabled:opacity-60 disabled:cursor-not-allowed"
              >
                <option value="">Default mic</option>
                {devices.map((d) => (
                  <option key={d.index} value={d.index}>{d.name}</option>
                ))}
              </select>
            </label>
          )}

          {mode !== "mic" && outputDevices.length > 0 && (
            <label
              className="flex items-center gap-1.5 text-xs text-gray-500 min-w-0"
              title={platform === "darwin"
              ? "System-audio source (BlackHole / virtual device). Captures whatever's playing — use this for Zoom/Teams/Meet participants, video playback, etc."
              : "System-audio source (WASAPI loopback). Captures whatever's playing through the selected speaker — use this for Zoom/Teams/Meet participants, video playback, etc."
            }
            >
              <Volume2 size={12} className="text-gray-500 flex-shrink-0" />
              <select
                value={loopbackIndex ?? ""}
                onChange={(e) => persistLoopback(e.target.value ? parseInt(e.target.value) : undefined)}
                disabled={isRecording}
                className="text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded px-2 py-1 outline-none min-w-0 w-full sm:w-[180px] lg:w-[260px] 2xl:w-[330px] disabled:opacity-60 disabled:cursor-not-allowed"
              >
                <option value="">Default speaker</option>
                {outputDevices.map((d) => (
                  <option key={d.index} value={d.index}>{d.name}</option>
                ))}
              </select>
            </label>
          )}
        </div>

        {isRecording ? (
          <button
            onClick={handleStop}
            disabled={loading}
            title={autoStopRemaining !== null
              ? `Auto-stop in ${autoStopRemaining}s unless someone speaks. Click to stop now.`
              : undefined}
            className={`flex items-center gap-2 px-3 py-1.5 disabled:opacity-50 text-white text-sm rounded-lg transition-colors flex-shrink-0 ${
              autoStopUrgent
                ? "bg-amber-600 hover:bg-amber-700 animate-pulse"
                : "bg-red-600 hover:bg-red-700"
            }`}
          >
            {autoStopRemaining !== null ? <Hourglass size={14} /> : <Square size={14} />}
            {autoStopRemaining !== null ? (
              <>
                <span className="hidden sm:inline font-mono">Stop in {fmt(autoStopRemaining)}</span>
                <span className="sm:hidden font-mono">{fmt(autoStopRemaining)}</span>
              </>
            ) : (
              <>
                <span className="hidden sm:inline">Stop Recording</span>
                <span className="sm:hidden">Stop</span>
              </>
            )}
          </button>
        ) : (
          <button
            onClick={handleStart}
            disabled={loading}
            className="flex items-center gap-2 px-3 py-1.5 bg-brand-600 hover:bg-brand-700 disabled:opacity-50 text-white text-sm rounded-lg transition-colors flex-shrink-0"
          >
            <Mic size={14} />
            <span className="hidden sm:inline">Start Recording</span>
            <span className="sm:hidden">Start</span>
          </button>
        )}
      </div>
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
                {platform === "darwin" ? "Open macOS Privacy Settings" : "Open Windows mic settings"}
              </button>
            )}
          </div>
        </div>
      </div>
    )}
    </MicAudioProvider>
  );
}

