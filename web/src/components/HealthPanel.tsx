import { memo } from "react";
import { fmtDateTime, fmtTimeSec, fmtUptime } from "../lib/format";
import { useAppState } from "../state/store";
import type { Activity, Job } from "../types";

export function HealthPanel() {
  return (
    <div className="health" data-testid="health-panel">
      <HealthStats />
      <section className="side-section">
        <h3>Jobs</h3>
        <JobsList />
      </section>
      <section className="side-section">
        <h3>Activity</h3>
        <ActivityLog />
      </section>
    </div>
  );
}

const HealthStats = memo(function HealthStats() {
  const h = useAppState((s) => s.health);
  const restarts = useAppState((s) => s.recorder?.helper_restarts ?? 0);
  const run = useAppState((s) => s.recorder?.helper_run ?? 0);
  if (!h) return <div className="empty">No health data yet.</div>;
  const warnText = h.warnings.join(" ").toLowerCase();
  const warnIf = (...words: string[]) => words.some((w) => warnText.includes(w));
  const rows: { label: string; value: string; warn?: boolean; title?: string }[] = [
    { label: "Uptime", value: fmtUptime(h.uptime_s) },
    { label: "Server memory", value: `${h.server_rss_mb.toFixed(0)} MB`, warn: warnIf("server memory") },
    {
      label: "Helper memory",
      value: h.helper_rss_mb === null ? "–" : `${h.helper_rss_mb.toFixed(0)} MB`,
      warn: warnIf("helper memory"),
    },
    { label: "Helper PID", value: h.helper_pid === null ? "not running" : String(h.helper_pid) },
    { label: "Helper run / restarts", value: `#${run} / ${restarts}`, warn: restarts > 0 },
    { label: "Event loop lag", value: `${h.event_loop_lag_ms.toFixed(0)} ms`, warn: h.event_loop_lag_ms > 250 || warnIf("lag") },
    { label: "WebSocket clients", value: String(h.ws_clients) },
    { label: "Jobs pending / running", value: `${h.jobs_pending} / ${h.jobs_running}` },
    { label: "Errors (last hour)", value: String(h.errors_last_hour), warn: h.errors_last_hour > 0 },
    { label: "Disk free", value: `${h.disk_free_gb.toFixed(1)} GB`, warn: h.disk_free_gb < 5 || warnIf("disk") },
  ];
  return (
    <>
      {h.warnings.length > 0 && (
        <ul className="warnings" role="alert">
          {h.warnings.map((w, i) => (
            <li key={i} data-testid="health-warning">
              {w}
            </li>
          ))}
        </ul>
      )}
      <dl className="stats">
        {rows.map((r) => (
          <div key={r.label} className={`stat${r.warn ? " stat-warn" : ""}`}>
            <dt>{r.label}</dt>
            <dd className="tabular">{r.value}</dd>
          </div>
        ))}
      </dl>
      {h.last_error && (
        <div className="last-error" title={h.last_error}>
          <span className="muted small">Last error</span>
          <div>{h.last_error}</div>
        </div>
      )}
    </>
  );
});

const JobsList = memo(function JobsList() {
  const jobs = useAppState((s) => s.jobs);
  const meetings = useAppState((s) => s.meetings);
  if (jobs.length === 0) return <div className="empty small">No jobs.</div>;
  const ordered = [...jobs].reverse().slice(0, 25);
  return (
    <ul className="jobs">
      {ordered.map((j) => (
        <JobRow key={j.id} job={j} target={meetings[j.target]?.title ?? j.target} />
      ))}
    </ul>
  );
});

function JobRow({ job, target }: { job: Job; target: string }) {
  return (
    <li className={`job job-${job.status}`} title={job.error ?? undefined} data-testid="job-row">
      <span className={`pill pill-${job.status}`}>{job.status}</span>
      <span className="job-kind">{job.kind === "notes" ? "Notes" : "Continuation"}</span>
      <span className="job-target" title={job.target}>
        {target}
      </span>
      <span className="muted small tabular">
        {fmtDateTime(job.created_at)}
        {job.attempts > 1 ? ` · try ${job.attempts}` : ""}
      </span>
      {job.error && <span className="job-error">{job.error}</span>}
    </li>
  );
}

// Stable React keys for activity entries (they have no id of their own).
const activityKeys = new WeakMap<Activity, number>();
let activitySeq = 0;
function activityKey(a: Activity): number {
  let k = activityKeys.get(a);
  if (k === undefined) {
    k = ++activitySeq;
    activityKeys.set(a, k);
  }
  return k;
}

const ActivityLog = memo(function ActivityLog() {
  const activity = useAppState((s) => s.activity);
  if (activity.length === 0) return <div className="empty small">No activity yet.</div>;
  const ordered = activity.slice().reverse();
  return (
    <ol className="activity" data-testid="activity-log">
      {ordered.map((a) => (
        <ActivityRow key={activityKey(a)} a={a} />
      ))}
    </ol>
  );
});

const ActivityRow = memo(function ActivityRow({ a }: { a: Activity }) {
  return (
    <li className={`act act-${a.level}`}>
      <time className="tabular muted" dateTime={a.ts}>
        {fmtTimeSec(a.ts)}
      </time>
      <span className="act-msg">{a.message}</span>
    </li>
  );
});
