import { useEffect, useRef, useState } from "react";

// Dev: Vite proxies /ws to the sidecar, so a same-origin URL works.
// Prod: Tauri webview origin is `tauri://localhost` — connect to the sidecar
// directly. Must match `SIDECAR_HTTP_BASE` in lib/api.ts.
const SIDECAR_WS_URL = import.meta.env.DEV ? null : "ws://127.0.0.1:8765/ws";

export type WSMessage =
  | { type: "utterances"; meeting_id: string; data: { id?: string; speaker: string; text: string; start_time: number; end_time: number; match_distance?: number | null }[] }
  | { type: "partial_utterance"; meeting_id: string; speaker: string; text: string }
  | { type: "status"; event: string; message?: string; meeting_id?: string; vault_path?: string }
  // ~30Hz while recording. `rms` and `peak` are both in [0, 1], computed
  // off the same 16kHz mono blocks that feed Whisper — so the UI
  // visualizers reflect the signal that's actually being transcribed
  // (mic + AEC-cancelled loopback, when system audio is enabled),
  // not just the raw mic via getUserMedia.
  | { type: "audio_level"; rms: number; peak: number }
  // ~5Hz while the auto-capture monitor is running. `state` is the
  // monitor's current node in its state machine and `confidence` is a
  // 0–1 EMA-smoothed Silero VAD output. Rendered as the small "Auto"
  // chip on the RecordingBar.
  | {
      type: "auto_capture";
      enabled: boolean;
      state: "disabled" | "listening" | "armed" | "recording" | "error";
      confidence: number;
      silent_seconds?: number;
    }
  // Fired by the live title-refinement loop when it has a better
  // suggestion (only when title_locked is false). The frontend patches
  // `liveMeeting.title` in place and shows a subtle fade so the user
  // notices the change without it feeling jarring.
  | {
      type: "title_updated";
      meeting_id: string;
      title: string;
      // "live_refinement" today; reserved for future sources (e.g.
      // "ai_summary" if we want to echo that through WS too).
      source?: string;
    };

type Handler = (msg: WSMessage) => void;

export function useWebSocket(onMessage: Handler): { connected: boolean } {
  // Keep a live reference to the handler so onmessage always calls the
  // latest closure, without needing to reattach.
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    // Using refs here is a trap under React 19 StrictMode — the
    // intentional double-mount would leave an orphan reconnect timer
    // from the first mount's onclose pointing at a stale socket, which
    // is how we were producing duplicate broadcasts. Closure-scoped
    // state + a cancel flag keeps everything local to one mount.
    let cancelled = false;
    let socket: WebSocket | null = null;
    let retry: number | null = null;

    const connect = () => {
      if (cancelled) return;
      const url = SIDECAR_WS_URL ?? (() => {
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        return `${proto}://${window.location.host}/ws`;
      })();
      socket = new WebSocket(url);

      socket.onopen = () => {
        if (!cancelled) setConnected(true);
      };

      socket.onmessage = (e) => {
        try {
          handlerRef.current(JSON.parse(e.data) as WSMessage);
        } catch {
          // ignore non-JSON frames
        }
      };

      socket.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        retry = window.setTimeout(connect, 2000);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (retry !== null) window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { connected };
}
