import { useEffect, useRef, useState } from "react";
import { useMicAudio } from "../lib/MicAudioContext";

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
  const [level, setLevel] = useState(0);
  const rafRef = useRef<number>(0);
  const smoothRef = useRef(0);

  useEffect(() => {
    if (!analyser) return;
    const buf = new Uint8Array(analyser.fftSize);

    const tick = () => {
      analyser.getByteTimeDomainData(buf);

      let sumSq = 0;
      for (let i = 0; i < buf.length; i++) {
        const s = (buf[i] - 128) / 128;
        sumSq += s * s;
      }
      const rms = Math.sqrt(sumSq / buf.length);
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
  }, [analyser]);

  if (error) {
    return <span className="text-xs text-gray-600">Mic unavailable</span>;
  }

  const activeCount = Math.round((level / 100) * BARS);
  return (
    <div className="flex items-end gap-px flex-shrink-0">
      {Array.from({ length: BARS }, (_, i) => (
        <div
          key={i}
          className={`w-1 rounded-sm ${barColor(i, i < activeCount)}`}
          style={{ height: `${10 + (i / (BARS - 1)) * 14}px` }}
        />
      ))}
    </div>
  );
}
