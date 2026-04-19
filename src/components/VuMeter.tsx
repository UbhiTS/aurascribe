import { useEffect, useRef, useState } from "react";
import { useMicAudio } from "../lib/MicAudioContext";
import { useSidecarAudioLevel, FRESHNESS_MS } from "../lib/useSidecarAudioLevel";

const BARS = 16;
const GREEN_BARS = 10;
const YELLOW_BARS = 3; // bars 10–12; bars 13–15 are red

function barColor(index: number, active: boolean): string {
  if (!active) return "bg-gray-700/60";
  if (index < GREEN_BARS) return "bg-green-500";
  if (index < GREEN_BARS + YELLOW_BARS) return "bg-yellow-400";
  return "bg-red-500";
}

export function VuMeter() {
  const { analyser, error } = useMicAudio();
  const { ref: sidecarLevelRef, active: sidecarActive } = useSidecarAudioLevel();
  const [level, setLevel] = useState(0);
  const rafRef = useRef<number>(0);
  const smoothRef = useRef(0);

  useEffect(() => {
    const buf = analyser ? new Uint8Array(analyser.fftSize) : null;

    const tick = () => {
      // Prefer sidecar-emitted level when a recent event is in hand —
      // that reflects the real mixed signal (mic + AEC-cancelled
      // loopback) instead of just the browser's mic capture. Falls back
      // to the analyser when idle or on a WS hiccup.
      let rms = 0;
      const sc = sidecarLevelRef.current;
      if (sc && performance.now() - sc.t < FRESHNESS_MS) {
        rms = sc.rms;
      } else if (buf && analyser) {
        analyser.getByteTimeDomainData(buf);
        let sumSq = 0;
        for (let i = 0; i < buf.length; i++) {
          const s = (buf[i] - 128) / 128;
          sumSq += s * s;
        }
        rms = Math.sqrt(sumSq / buf.length);
      }
      const raw = Math.min(100, rms * 800);

      const prev = smoothRef.current;
      smoothRef.current = raw > prev
        ? 0.3 * raw + 0.7 * prev   // attack
        : 0.05 * raw + 0.95 * prev; // release

      setLevel(smoothRef.current);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);

    return () => cancelAnimationFrame(rafRef.current);
  }, [analyser, sidecarLevelRef]);

  // Only surface the "mic unavailable" affordance when we genuinely have
  // nothing to visualize — if the sidecar is feeding levels, recording is
  // live and the bars are accurate, browser-mic permission be damned.
  if (error && !sidecarActive) {
    return <span className="text-xs text-gray-600">Mic unavailable</span>;
  }

  const activeCount = Math.round((level / 100) * BARS);
  return (
    <div className="flex items-end gap-px flex-shrink-0">
      {Array.from({ length: BARS }, (_, i) => (
        <div
          key={i}
          className={`w-1 rounded-sm ${barColor(i, i < activeCount)}`}
          style={{ height: `${10 + i}px` }}
        />
      ))}
    </div>
  );
}
