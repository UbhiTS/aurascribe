import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

type State = { analyser: AnalyserNode | null; error: boolean };

const Ctx = createContext<State>({ analyser: null, error: false });

function normalize(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// Pick the browser-enumerated audioinput whose label best matches the backend
// device name. Browser labels often include a prefix like "Microphone (...)"
// while sounddevice strips it, so we score by shared word tokens.
function pickDeviceId(devices: MediaDeviceInfo[], wanted: string | null): string | undefined {
  if (!wanted) return undefined;
  const wantedTokens = new Set(normalize(wanted).split(" ").filter((t) => t.length >= 3));
  if (wantedTokens.size === 0) return undefined;

  let bestId: string | undefined;
  let bestScore = 0;
  for (const d of devices) {
    if (d.kind !== "audioinput" || !d.label) continue;
    const labelTokens = normalize(d.label).split(" ").filter((t) => t.length >= 3);
    let score = 0;
    for (const t of labelTokens) if (wantedTokens.has(t)) score++;
    if (score > bestScore) {
      bestScore = score;
      bestId = d.deviceId;
    }
  }
  return bestScore > 0 ? bestId : undefined;
}

export function MicAudioProvider({
  children,
  deviceName,
}: {
  children: ReactNode;
  deviceName?: string | null;
}) {
  const [state, setState] = useState<State>({ analyser: null, error: false });
  const streamRef = useRef<MediaStream | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    let cancelled = false;

    const teardown = () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      ctxRef.current?.close().catch(() => {});
      ctxRef.current = null;
    };

    (async () => {
      try {
        // First request ensures labels are populated for enumerateDevices().
        const priming = await navigator.mediaDevices.getUserMedia({ audio: true });
        priming.getTracks().forEach((t) => t.stop());
        if (cancelled) return;

        const all = await navigator.mediaDevices.enumerateDevices();
        const deviceId = pickDeviceId(all, deviceName ?? null);

        const constraints: MediaStreamConstraints = {
          audio: {
            ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
            autoGainControl: false,
            noiseSuppression: false,
            echoCancellation: false,
          },
          video: false,
        };
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        if (cancelled) { stream.getTracks().forEach((t) => t.stop()); return; }

        const ctx = new AudioContext();
        if (ctx.state === "suspended") await ctx.resume();
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.3;
        ctx.createMediaStreamSource(stream).connect(analyser);

        streamRef.current = stream;
        ctxRef.current = ctx;
        setState({ analyser, error: false });

        const resume = () => { ctx.state === "suspended" && ctx.resume(); };
        document.addEventListener("click", resume, { once: true });
      } catch {
        if (!cancelled) setState({ analyser: null, error: true });
      }
    })();

    return () => {
      cancelled = true;
      teardown();
    };
  }, [deviceName]);

  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

export const useMicAudio = () => useContext(Ctx);
