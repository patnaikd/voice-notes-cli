import { useState } from "react";
import { useAppState } from "../state/store";
import { HealthPanel } from "./HealthPanel";
import { SpeakersPanel } from "./SpeakersPanel";

type Tab = "health" | "speakers";

export function SidePanel({ collapsed = false, onToggle }: { collapsed?: boolean; onToggle?: () => void }) {
  const [tab, setTab] = useState<Tab>("health");
  const warnings = useAppState((s) => s.health?.warnings.length ?? 0);
  const speakers = useAppState((s) => s.speakers.length);

  if (collapsed) {
    // A slim rail keeps warnings visible; clicking a label reopens the panel on that tab.
    const open = (next: Tab) => {
      setTab(next);
      onToggle?.();
    };
    return (
      <div className="panel side-rail" data-testid="side-rail">
        <button className="icon-btn side-toggle" aria-label="Show side panel" title="Show side panel" onClick={onToggle} data-testid="side-toggle">
          ‹
        </button>
        <button className="side-rail-tab" onClick={() => open("health")} title="Health">
          Health
          {warnings > 0 && <span className="tab-badge tab-badge-warn">{warnings}</span>}
        </button>
        <button className="side-rail-tab" onClick={() => open("speakers")} title="Speakers">
          Speakers
        </button>
      </div>
    );
  }

  return (
    <div className="panel side-panel">
      <div className="tabs side-tabs" role="tablist">
        <button role="tab" className="tab" aria-selected={tab === "health"} onClick={() => setTab("health")} data-testid="tab-health">
          Health
          {warnings > 0 && <span className="tab-badge tab-badge-warn">{warnings}</span>}
        </button>
        <button role="tab" className="tab" aria-selected={tab === "speakers"} onClick={() => setTab("speakers")} data-testid="tab-speakers">
          Speakers
          {speakers > 0 && <span className="tab-badge">{speakers}</span>}
        </button>
        {onToggle && (
          <button className="icon-btn side-toggle" aria-label="Hide side panel" title="Hide side panel" onClick={onToggle} data-testid="side-toggle">
            ›
          </button>
        )}
      </div>
      <div className="side-body">{tab === "health" ? <HealthPanel /> : <SpeakersPanel />}</div>
    </div>
  );
}
