import { useEffect, useRef, useState } from "react";

export type WSMessage =
  | { type: "utterances"; meeting_id: string; data: { id?: string; speaker: string; text: string; start_time: number; end_time: number; match_distance?: number | null }[] }
  | { type: "partial_utterance"; meeting_id: string; speaker: string; text: string }
  | { type: "status"; event: string; message?: string; meeting_id?: string; vault_path?: string };

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
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${window.location.host}/ws`);

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
