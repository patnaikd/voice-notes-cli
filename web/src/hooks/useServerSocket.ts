import { useEffect } from "react";
import type { Store } from "../state/store";
import { levelStore } from "../state/levelStore";
import type { ServerMessage } from "../types";

export const BACKOFF_MS = [1000, 2000, 5000, 10000];
/** The server pings every 15 s; treat 45 s of silence as a dead socket (e.g. after sleep). */
const STALE_MS = 45_000;

export function wsUrl(loc: Location = window.location): string {
  const proto = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${loc.host}/ws`;
}

/**
 * Connects to /ws, feeds messages into the store and reconnects with backoff
 * (1, 2, 5, 10 s). Every (re)connect starts from the server's `snapshot`.
 */
export function useServerSocket(store: Store, url: string = wsUrl()): void {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let attempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let staleTimer: ReturnType<typeof setInterval> | undefined;
    let lastMessageAt = Date.now();
    let disposed = false;

    const scheduleReconnect = () => {
      if (disposed || retryTimer) return;
      store.dispatch({ type: "conn", status: "reconnecting" });
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt++;
      retryTimer = setTimeout(() => {
        retryTimer = undefined;
        connect();
      }, delay);
    };

    const connect = () => {
      if (disposed) return;
      try {
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      const sock = ws;
      sock.onopen = () => {
        lastMessageAt = Date.now();
      };
      sock.onmessage = (ev) => {
        lastMessageAt = Date.now();
        let msg: ServerMessage;
        try {
          msg = JSON.parse(typeof ev.data === "string" ? ev.data : "");
        } catch {
          return;
        }
        if (!msg || typeof msg.type !== "string") return;
        switch (msg.type) {
          case "levels":
            levelStore.set(msg.levels);
            return;
          case "ping":
            if (sock.readyState === WebSocket.OPEN) sock.send('{"type":"pong"}');
            return;
          case "snapshot":
            attempt = 0;
            store.dispatch(msg);
            store.dispatch({ type: "conn", status: "live" });
            return;
          default:
            store.dispatch(msg);
        }
      };
      sock.onclose = () => {
        if (ws === sock) ws = null;
        levelStore.reset();
        scheduleReconnect();
      };
      sock.onerror = () => {
        try {
          sock.close();
        } catch {
          /* already closed */
        }
      };
    };

    staleTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN && Date.now() - lastMessageAt > STALE_MS) {
        ws.close();
      }
    }, 5000);

    // Reconnect immediately when the page comes back to the foreground.
    const onVisible = () => {
      if (document.visibilityState === "visible" && !ws && retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = undefined;
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    connect();
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisible);
      clearTimeout(retryTimer);
      clearInterval(staleTimer);
      if (ws) {
        const sock = ws;
        sock.onclose = null;
        sock.onmessage = null;
        sock.onerror = null;
        // Closing a socket that is still connecting logs a browser warning; close it once open.
        if (sock.readyState === WebSocket.CONNECTING) sock.onopen = () => sock.close();
        else sock.close();
      }
    };
  }, [store, url]);
}
