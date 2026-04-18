import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

type State = { analyser: AnalyserNode | null; error: boolean };

const Ctx = createContext<State>({ analyser: null, error: false });

export function MicAudioProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ analyser: null, error: false });
  const streamRef = useRef<MediaStream | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { autoGainControl: false, noiseSuppression: false, echoCancellation: false },
          video: false,
        });
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
      streamRef.current?.getTracks().forEach((t) => t.stop());
      ctxRef.current?.close();
    };
  }, []);

  return <Ctx.Provider value={state}>{children}</Ctx.Provider>;
}

export const useMicAudio = () => useContext(Ctx);
