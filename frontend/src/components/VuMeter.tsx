import { useEffect, useRef, useState } from "react";

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
  const [level, setLevel]         = useState(0);          // 0–100, smoothed
  const [error, setError]         = useState(false);

  const rafRef        = useRef<number>(0);
  const ctxRef        = useRef<AudioContext | null>(null);
  const smoothRef     = useRef(0);                        // exponential moving average

  useEffect(() => {
    let stream: MediaStream;

    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { autoGainControl: false, noiseSuppression: false, echoCancellation: false },
          video: false,
        });
        const ctx = new AudioContext();
        ctxRef.current = ctx;
        // Browsers suspend AudioContext until user interaction — resume immediately
        if (ctx.state === "suspended") await ctx.resume();
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        ctx.createMediaStreamSource(stream).connect(analyser);

        const buf = new Uint8Array(analyser.fftSize);

        const tick = () => {
          analyser.getByteTimeDomainData(buf);

          // RMS of waveform (128 = silence)
          let sumSq = 0;
          for (let i = 0; i < buf.length; i++) {
            const s = (buf[i] - 128) / 128;
            sumSq += s * s;
          }
          const rms = Math.sqrt(sumSq / buf.length);
          const raw = Math.min(100, rms * 800);

          // Exponential smoothing — fast attack (0.3), slow release (0.05)
          const prev = smoothRef.current;
          smoothRef.current = raw > prev
            ? 0.3 * raw + 0.7 * prev   // attack
            : 0.05 * raw + 0.95 * prev; // release

          const smoothed = smoothRef.current;
          setLevel(smoothed);


          rafRef.current = requestAnimationFrame(tick);
        };
        rafRef.current = requestAnimationFrame(tick);

        // Resume context on first user gesture in case browser suspended it
        const resume = () => { ctx.state === "suspended" && ctx.resume(); };
        document.addEventListener("click", resume, { once: true });
      } catch {
        setError(true);
      }
    })();

    return () => {
      cancelAnimationFrame(rafRef.current);
      stream?.getTracks().forEach((t) => t.stop());
      ctxRef.current?.close();
    };
  }, []);

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
