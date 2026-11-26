import { useState } from "react";
import { useAppState } from "../state/store";
import { HealthPanel } from "./HealthPanel";
import { SpeakersPanel } from "./SpeakersPanel";

type Tab = "health" | "speakers";

export function SidePanel() {
  const [tab, setTab] = useState<Tab>("health");
  const warnings = useAppState((s) => s.health?.warnings.length ?? 0);
  const speakers = useAppState((s) => s.speakers.length);
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
      </div>
      <div className="side-body">{tab === "health" ? <HealthPanel /> : <SpeakersPanel />}</div>
    </div>
  );
}
