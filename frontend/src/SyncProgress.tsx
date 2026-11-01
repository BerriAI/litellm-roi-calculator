import { useState } from "react";
import { gitTerms } from "./configuration";
import type { AppState } from "./api";

function duration(seconds: number) {
  if (seconds < 60) return `${Math.floor(seconds)} sec`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} hr${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
}

export function SyncProgress({ status, provider = "github" }: { status: AppState["status"]; provider?: string }) {
  const terms = gitTerms(provider);
  const known = status.total > 0;
  const percent = known ? Math.round(100 * status.done / status.total) : null;
  return <div className="grid gap-3 min-w-0" role="status" aria-live="polite">
    <div className="flex items-center justify-between gap-4 text-sm">
      <span>{status.stage === "idle" ? "Starting…" : status.stage}</span>
      {percent !== null && <span className="tabular-nums muted">{percent}%</span>}
    </div>
    <progress className="sync-progress" max={known ? status.total : 1}
      value={known ? status.done : status.running ? undefined : 0}
      aria-label={known ? `Number of ${terms.prsLower} processed` : status.stage}
      aria-valuetext={known ? `${status.done} of ${status.total} ${terms.prsLower} processed` : status.stage} />
    {known && <p className="text-xs muted">{status.done} of {status.total} {terms.prs} processed
      {(status.reused || 0) > 0 && ` · ${status.reused} reused`}
      {status.estimated > (status.reused || 0) && ` · ${status.estimated - (status.reused || 0)} newly estimated`}
      {status.needs_attention > 0 && ` · ${status.needs_attention} need attention`}</p>}
    <p className="text-xs muted tabular-nums">{duration(status.elapsed_seconds || 0)} elapsed
      {status.running && <span>{" · "}{status.remaining_seconds == null ? "Estimating time remaining…" :
        status.remaining_seconds < 60 ? "Less than a minute remaining" : `About ${duration(status.remaining_seconds)} remaining`}</span>}
    </p>
  </div>;
}
