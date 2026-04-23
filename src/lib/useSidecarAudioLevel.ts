import { useEffect, useRef, useState } from "react";

// Browser CustomEvent detail shape (also see App.tsx handler).
export interface AudioLevelDetail {
  rms: number;
  peak: number;
  t: number; // performance.now() at dispatch
}

// ~30Hz stream of audio levels the sidecar emits during recording. The
// RAF-driven visualizers (VuMeter / Waveform) need two things out of
// this: the latest sample (every frame, no re-render) and a coarse
// "is the sidecar currently feeding me?" signal (for UI fallbacks like
// the "Mic unavailable" message — suppressed while sidecar is live).
//
// Hence the mixed return: `ref` for the sample (consumed in RAF, no
// renders), `active` for the affordance gate (state, re-renders when
// recording starts/stops). Freshness window is generous so a brief
// WS hiccup doesn't flicker the meters back to the mic-only fallback.
export const FRESHNESS_MS = 500;

export function useSidecarAudioLevel(): {
  ref: React.MutableRefObject<AudioLevelDetail | null>;
  active: boolean;
} {
  const ref = useRef<AudioLevelDetail | null>(null);
  const [active, setActive] = useState(false);

  useEffect(() => {
    // Event-driven freshness timer. Arming on each audio_level arrival
    // means the only setState calls happen at the active↔idle edges,
    // not 4× per second while idle. The timer auto-extends as long as
    // events keep flowing, and expires (flipping active=false) once
    // the stream has been quiet for FRESHNESS_MS.
    let idleTimer: number | null = null;
    const armIdle = () => {
      if (idleTimer !== null) window.clearTimeout(idleTimer);
      idleTimer = window.setTimeout(() => {
        setActive((prev) => (prev ? false : prev));
        idleTimer = null;
      }, FRESHNESS_MS);
    };

    const onLevel = (e: Event) => {
      const ce = e as CustomEvent<AudioLevelDetail>;
      ref.current = ce.detail;
      setActive((prev) => (prev ? prev : true));
      armIdle();
    };
    window.addEventListener("aurascribe:audio-level", onLevel);

    return () => {
      window.removeEventListener("aurascribe:audio-level", onLevel);
      if (idleTimer !== null) window.clearTimeout(idleTimer);
      ref.current = null;
    };
  }, []);

  return { ref, active };
}
