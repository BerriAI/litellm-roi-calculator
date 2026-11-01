import type { ReactNode } from "react";
import type { Settings } from "./api";

export const gitTerms = (provider: string) => provider === "gitlab"
  ? { host: "GitLab", pr: "Merge request", prs: "Merge requests", prsLower: "merge requests",
      noMatch: "No GitLab match", matchHelp: "Matches use the author's public GitLab profile email or commit emails associated with their GitLab account. Email matching ignores case. Private, noreply, and ambiguous emails stay unmatched. Click a GitLab username to edit its match. Manual matches take priority." }
  : provider === "azure_devops"
  ? { host: "Azure DevOps", pr: "Pull request", prs: "Pull requests", prsLower: "pull requests",
      noMatch: "No Azure DevOps match", matchHelp: "Matches use the author's Azure DevOps account email or commit emails associated with the same Azure DevOps account. Email matching ignores case. Ambiguous emails stay unmatched. Click a username to edit its match. Manual matches take priority." }
  : { host: "GitHub", pr: "Pull request", prs: "Pull requests", prsLower: "pull requests",
      noMatch: "No GitHub match", matchHelp: "Matches use the author's public GitHub email or commit emails associated with their GitHub account. Email matching ignores case. Private, noreply, and ambiguous emails stay unmatched. Click a GitHub username to edit its match. Manual matches take priority." };

export type FormValues = {
  gateway_url: string; admin_key: string; estimator_key: string; github_token: string;
  git_provider: string; github_api_url: string; github_connection: string;
  azure_devops_org: string; azure_devops_pat: string;
  gitlab_url: string; gitlab_pat: string;
  repos: string; estimator_model: string; estimator_prompt: string;
  backfill_days: string; update_interval_hours: string;
};

export const formValues = (s: Settings): FormValues => ({
  gateway_url: s.gateway_url, admin_key: "", estimator_key: "", github_token: "",
  git_provider: s.git_provider, github_api_url: s.github_api_url, github_connection: s.github_connection,
  azure_devops_org: s.azure_devops_org, azure_devops_pat: "",
  gitlab_url: s.gitlab_url, gitlab_pat: "",
  repos: s.repos.join("\n"), estimator_model: s.estimator_model,
  estimator_prompt: s.estimator_prompt, backfill_days: String(s.backfill_days),
  update_interval_hours: String(s.update_interval_minutes / 60),
});

export function settingsUpdate(values: FormValues, locked: string[], fields = Object.keys(values)) {
  return Object.fromEntries(fields.filter(name => !locked.includes(name)).map(name => {
    const value = values[name as keyof FormValues];
    if (name === "repos") return [name, value.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean)];
    if (name === "update_interval_hours") return ["update_interval_minutes", Math.round(Number(value) * 60)];
    if (name === "backfill_days") return [name, Number(value)];
    return [name, value];
  }));
}

export function Field({ label, help, children, locked = false }: {
  label: string; help?: string; children: ReactNode; locked?: boolean;
}) {
  return <label className="field"><span>{label} {locked && <span className="muted text-xs font-normal">(managed by server)</span>}</span>
    {children}{help && <span className="field-help">{help}</span>}</label>;
}
