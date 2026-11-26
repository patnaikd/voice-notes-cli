import { useCallback, useEffect, useMemo, useState } from "react";
import { setApiErrorHandler } from "./api/client";
import { LivePanel } from "./components/LivePanel";
import { MeetingDrawer } from "./components/MeetingDrawer";
import { MeetingsTimeline } from "./components/MeetingsTimeline";
import { RecorderBar } from "./components/RecorderBar";
import { SidePanel } from "./components/SidePanel";
import { StatusBanner } from "./components/StatusBanner";
import { Toasts } from "./components/Toasts";
import { useServerSocket } from "./hooks/useServerSocket";
import { createStore, StoreContext, type Store } from "./state/store";

export function App({ store: injected, connect = true }: { store?: Store; connect?: boolean }) {
  const store = useMemo(() => injected ?? createStore(), [injected]);
  return (
    <StoreContext.Provider value={store}>
      {connect && <SocketConnector store={store} />}
      <Shell store={store} />
    </StoreContext.Provider>
  );
}

function SocketConnector({ store }: { store: Store }) {
  useServerSocket(store);
  return null;
}

function Shell({ store }: { store: Store }) {
  const [openMeeting, setOpenMeeting] = useState<string | null>(null);
  const closeDrawer = useCallback(() => setOpenMeeting(null), []);
  const [sideCollapsed, setSideCollapsed] = useState(() => readFlag(SIDE_KEY));
  const toggleSide = useCallback(() => {
    setSideCollapsed((collapsed) => {
      writeFlag(SIDE_KEY, !collapsed);
      return !collapsed;
    });
  }, []);

  useEffect(() => {
    setApiErrorHandler((message) => store.dispatch({ type: "toast", level: "error", message }));
    return () => setApiErrorHandler(null);
  }, [store]);

  return (
    <div className="app">
      <RecorderBar />
      <StatusBanner />
      <main className={`layout${sideCollapsed ? " layout-side-collapsed" : ""}`}>
        <section className="col col-live" aria-label="Live transcript">
          <LivePanel />
        </section>
        <section className="col col-meetings" aria-label="Meetings">
          <MeetingsTimeline onOpen={setOpenMeeting} />
        </section>
        <aside className="col col-side" aria-label="Speakers and health">
          <SidePanel collapsed={sideCollapsed} onToggle={toggleSide} />
        </aside>
      </main>
      {openMeeting && <MeetingDrawer meetingId={openMeeting} onClose={closeDrawer} />}
      <Toasts />
    </div>
  );
}

const SIDE_KEY = "voice-notes.side-collapsed";

function readFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function writeFlag(key: string, value: boolean): void {
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* private mode or storage disabled */
  }
}
