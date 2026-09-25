import type { AppState } from "./api";

export function SyncProgress({ status }: { status: AppState["status"] }) {
  const known = status.total > 0;
  const percent = known ? Math.round(100 * status.done / status.total) : null;
  return <div className="grid gap-3 min-w-0" role="status" aria-live="polite">
    <div className="flex items-center justify-between gap-4 text-sm">
      <span>{status.stage === "idle" ? "Starting…" : status.stage}</span>
      {percent !== null && <span className="tabular-nums muted">{percent}%</span>}
    </div>
    <progress className="sync-progress" max={known ? status.total : 1}
      value={known ? status.done : status.running ? undefined : 0}
      aria-label={known ? "Pull requests processed" : status.stage}
      aria-valuetext={known ? `${status.done} of ${status.total} PRs processed` : status.stage} />
    {known && <p className="text-xs muted">{status.done} of {status.total} PRs processed
      {status.needs_attention > 0 && ` · ${status.needs_attention} need attention`}</p>}
  </div>;
}
