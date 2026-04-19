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
    const onLevel = (e: Event) => {
      const ce = e as CustomEvent<AudioLevelDetail>;
      ref.current = ce.detail;
      setActive(true);
    };
    window.addEventListener("aurascribe:audio-level", onLevel);

    // Flip `active` back off when no event has arrived within the
    // freshness window — cheap 4Hz poll, well below any meaningful
    // re-render cost.
    const interval = window.setInterval(() => {
      const cur = ref.current;
      if (!cur || performance.now() - cur.t >= FRESHNESS_MS) {
        setActive((prev) => (prev ? false : prev));
      }
    }, 250);

    return () => {
      window.removeEventListener("aurascribe:audio-level", onLevel);
      window.clearInterval(interval);
      ref.current = null;
    };
  }, []);

  return { ref, active };
}
