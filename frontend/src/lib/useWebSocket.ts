import { useEffect, useRef, useCallback } from "react";

export type WSMessage =
  | { type: "utterances"; meeting_id: number; data: { speaker: string; text: string; start_time: number; end_time: number }[] }
  | { type: "status"; event: string; message?: string; meeting_id?: number; vault_path?: string };

type Handler = (msg: WSMessage) => void;

export function useWebSocket(onMessage: Handler) {
  const ws = useRef<WebSocket | null>(null);
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  const connect = useCallback(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${window.location.host}/ws`);

    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as WSMessage;
        handlerRef.current(msg);
      } catch {}
    };

    socket.onclose = () => {
      // Reconnect after 2 seconds
      setTimeout(connect, 2000);
    };

    ws.current = socket;
  }, []);

  useEffect(() => {
    connect();
    return () => {
      ws.current?.close();
    };
  }, [connect]);
}
