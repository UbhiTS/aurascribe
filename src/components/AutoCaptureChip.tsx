import { Radio } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";
import type { AutoCaptureState } from "../lib/api";

interface Props {
  state: AutoCaptureState | null;
  // Parent owns the state so the chip can update optimistically — the WS
  // echo arrives ~250ms later and overwrites with server truth either way.
  setState: (s: AutoCaptureState | null) => void;
}

// Rendered in the header using the same icon+text grammar as the other
// status items (Sidecar / AI / Obsidian / Whisper / Diarize), plus an
// iOS-style switch on the right that's the interactive affordance.
//
// Division of responsibilities between the visual elements:
//   * Switch  — user intent: on or off. Click target.
//   * Icon    — actual mic state. Color shifts through:
//       gray       = disabled (switch off)
//       emerald    = listening
//       red+pulse  = armed (speech detected, start_meeting firing)
//       red        = a meeting is recording
//       amber      = mic couldn't open (permission / in-use)
//   * Status  — only shown for the non-obvious sub-states (armed /
//                 recording / mic error); "listening" and "off" are
//                 already clear from the switch + icon so they'd just be
//                 noise.
//
// Five states map from the monitor's state machine:
//   disabled   — off. Click switch to enable.
//   listening  — hot, default when enabled.
//   armed      — sustained speech detected, start_meeting is firing.
//   recording  — a meeting is active (auto OR manual); switch is locked
//                because toggling mid-recording would be surprising.
//   error      — mic couldn't open. Monitor auto-retries on backoff.

export function AutoCaptureChip({ state, setState }: Props) {
  const [busy, setBusy] = useState(false);

  const onToggle = async () => {
    if (!state || busy) return;
    const nextEnabled = !state.enabled;
    setBusy(true);
    // Flip locally so the switch slides instantly; server echo corrects later.
    setState({
      ...state,
      enabled: nextEnabled,
      state: nextEnabled ? "listening" : "disabled",
    });
    try {
      const fresh = await api.autoCapture.setEnabled(nextEnabled);
      setState(fresh);
    } catch {
      // Roll back and let the WS broadcast re-sync.
      setState(state);
    } finally {
      setBusy(false);
    }
  };

  if (!state) {
    // Pre-first-status placeholder — prevents layout shift.
    return (
      <div className="flex items-center gap-2 text-xs flex-shrink-0" title="Auto-capture loading…">
        <Radio size={13} className="text-gray-600" />
        <span className="text-gray-600 hidden lg:inline">Auto Recording</span>
        <Switch enabled={false} disabled />
      </div>
    );
  }

  const { enabled, state: kind } = state;
  const duringRecording = kind === "recording";
  // Mid-recording we still allow toggling: turning it off means "once this
  // recording stops, don't auto-start again" (the active recording itself
  // keeps going). `busy` is only about the in-flight HTTP round-trip.
  const switchDisabled = busy;

  const { subStatus, iconClass, subStatusClass, title } = (() => {
    if (!enabled) {
      return {
        subStatus: null,
        iconClass: "text-gray-500",
        subStatusClass: "",
        title:
          "Auto-capture is off. Flip the switch to turn it on — AuraScribe will start recording as soon as it hears sustained speech.",
      };
    }
    if (kind === "error") {
      return {
        subStatus: "mic error",
        iconClass: "text-amber-400",
        subStatusClass: "text-amber-300",
        title:
          "Auto-capture couldn't open the mic (permission / device in use). It will retry automatically.",
      };
    }
    if (kind === "armed") {
      return {
        subStatus: "starting",
        iconClass: "text-red-400 animate-pulse",
        subStatusClass: "text-red-300",
        title: "Sustained speech detected — starting a meeting now.",
      };
    }
    if (duringRecording) {
      // No "· recording" text — the RecordingBar already says it loudly
      // (red timer, Stop button). Red icon here is enough to keep the
      // header row consistent without being redundant.
      return {
        subStatus: null,
        iconClass: "text-red-400",
        subStatusClass: "",
        title: "Recording in progress. Toggle to control whether auto-capture resumes once this recording ends — the active recording itself keeps running until you stop it.",
      };
    }
    // listening
    return {
      subStatus: null,
      iconClass: "text-emerald-400",
      subStatusClass: "",
      title: "Auto-capture is listening. Flip the switch to turn it off.",
    };
  })();

  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={switchDisabled}
      title={title}
      role="switch"
      aria-checked={enabled}
      className="flex items-center gap-2 text-xs flex-shrink-0 transition-opacity disabled:cursor-default disabled:opacity-70"
    >
      <Radio size={13} className={iconClass} />
      {/* Label collapses under lg — icon + switch carry the interaction,
          tooltip carries the "Auto Recording" affordance. */}
      <span className="text-gray-300 hidden lg:inline">Auto Recording</span>
      {subStatus && (
        <>
          <span className="text-gray-600 hidden lg:inline">·</span>
          <span className={`${subStatusClass} hidden lg:inline`}>{subStatus}</span>
        </>
      )}
      <Switch enabled={enabled} disabled={switchDisabled} />
    </button>
  );
}

// Compact iOS-style toggle. Sized to sit comfortably against 13px icons
// and 12px text in the header without dominating the row.
function Switch({ enabled, disabled }: { enabled: boolean; disabled?: boolean }) {
  return (
    <span
      aria-hidden
      className={`relative inline-flex h-4 w-7 flex-shrink-0 rounded-full border transition-colors ${
        enabled
          ? "bg-emerald-500 border-emerald-400"
          : "bg-gray-800 border-gray-700"
      } ${disabled ? "" : "hover:brightness-110"}`}
    >
      <span
        className={`absolute top-0.5 h-2.5 w-2.5 rounded-full bg-white shadow-sm transition-transform ${
          enabled ? "translate-x-[14px]" : "translate-x-0.5"
        }`}
      />
    </span>
  );
}
