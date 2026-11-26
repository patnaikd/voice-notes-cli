import { useAppState } from "../state/store";

/** Thin banners for connection loss and recorder errors. */
export function StatusBanner() {
  const conn = useAppState((s) => s.conn);
  const hasSnapshot = useAppState((s) => s.hasSnapshot);
  const recState = useAppState((s) => s.recorder?.state);
  const message = useAppState((s) => s.recorder?.message ?? null);

  if (conn !== "live") {
    return (
      <div className="banner banner-warn" role="status">
        {hasSnapshot
          ? "Connection to the server lost. Reconnecting… (data shown may be stale)"
          : "Connecting to the Voice Notes server…"}
      </div>
    );
  }
  if (recState === "error") {
    return (
      <div className="banner banner-error" role="alert">
        Recorder error{message ? `: ${message}` : ""}
      </div>
    );
  }
  if (recState === "restarting") {
    return (
      <div className="banner banner-warn" role="status">
        Capture helper is restarting{message ? `: ${message}` : ""}. The current segment stays open.
      </div>
    );
  }
  return null;
}
