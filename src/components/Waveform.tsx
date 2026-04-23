import { useEffect, useRef } from "react";
import { useMicAudio } from "../lib/MicAudioContext";
import { useSidecarAudioLevel, FRESHNESS_MS } from "../lib/useSidecarAudioLevel";

// Scrolling amplitude history — one bar sampled every SAMPLE_MS, oldest drops off the left.
const BARS = 96;
const SAMPLE_MS = 50; // 20 Hz

// Match VuMeter thresholds: 10 green + 3 yellow + 3 red out of 16 bars.
const YELLOW_LEVEL = 62.5; // 10 / 16 * 100
const RED_LEVEL = 81.25; // 13 / 16 * 100

function levelColor(level: number, opacity: number): string {
  if (level < YELLOW_LEVEL) return `rgba(34, 197, 94, ${opacity})`; // green-500
  if (level < RED_LEVEL) return `rgba(250, 204, 21, ${opacity})`; // yellow-400
  return `rgba(239, 68, 68, ${opacity})`; // red-500
}

export function Waveform() {
  const { analyser, error } = useMicAudio();
  const { ref: sidecarLevelRef, active: sidecarActive } = useSidecarAudioLevel();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const history = useRef<number[]>(new Array(BARS).fill(0));

  useEffect(() => {
    const sampleBuf = analyser ? new Uint8Array(analyser.fftSize) : null;
    let rafId = 0;
    let lastSample = 0;

    const tick = () => {
      const now = performance.now();
      if (now - lastSample >= SAMPLE_MS) {
        lastSample = now;
        // Prefer sidecar-emitted level when fresh — same fallback
        // discipline as VuMeter so both visualizers stay in sync with
        // whatever the transcription pipeline is actually seeing.
        let rms = 0;
        const sc = sidecarLevelRef.current;
        if (sc && now - sc.t < FRESHNESS_MS) {
          rms = sc.rms;
        } else if (sampleBuf && analyser) {
          analyser.getByteTimeDomainData(sampleBuf);
          let sumSq = 0;
          for (let i = 0; i < sampleBuf.length; i++) {
            const s = (sampleBuf[i] - 128) / 128;
            sumSq += s * s;
          }
          rms = Math.sqrt(sumSq / sampleBuf.length);
        }
        // Same scale as VuMeter (rms * 800, capped 0–100) so color thresholds match.
        const level = Math.min(100, rms * 800);
        history.current.push(level);
        if (history.current.length > BARS) history.current.shift();
      }

      const canvas = canvasRef.current;
      if (canvas) {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const cw = Math.max(1, Math.floor(rect.width * dpr));
        const ch = Math.max(1, Math.floor(rect.height * dpr));
        if (canvas.width !== cw || canvas.height !== ch) {
          canvas.width = cw;
          canvas.height = ch;
        }
        const g = canvas.getContext("2d");
        if (g) {
          g.clearRect(0, 0, cw, ch);
          const barW = cw / BARS;
          const innerW = Math.max(1, barW * 0.55);
          const mid = ch / 2;
          const minH = Math.max(2 * dpr, ch * 0.06);

          for (let i = 0; i < BARS; i++) {
            const level = history.current[i]; // 0–100
            const barH = Math.max(minH, (level / 100) * ch);
            const x = i * barW + (barW - innerW) / 2;
            const y = mid - barH / 2;
            // Older bars (left) fade slightly; newest (right) is brightest.
            const opacity = 0.35 + 0.55 * (i / (BARS - 1));
            g.fillStyle = levelColor(level, opacity);
            g.fillRect(x, y, innerW, barH);
          }
        }
      }

      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);

    return () => cancelAnimationFrame(rafId);
  }, [analyser, sidecarLevelRef]);

  // Keep the waveform rendering if we have sidecar levels even when the
  // browser mic errored; the history gets populated from the sidecar feed.
  if (error && !sidecarActive) return null;
  // Stretch to absorb whatever horizontal slack the row has — when the
  // source controls fit on the same line, the waveform fills the gap;
  // when they wrap to a new line, it grows to fill the freed-up width.
  // The min-width keeps it readable before triggering a wrap.
  return <canvas ref={canvasRef} className="flex-1 min-w-[120px] h-8" />;
}
