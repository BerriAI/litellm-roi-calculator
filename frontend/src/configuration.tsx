import type { ReactNode } from "react";
import type { Settings } from "./api";

export type FormValues = {
  gateway_url: string; admin_key: string; estimator_key: string; github_token: string;
  github_api_url: string; github_connection: string; repos: string; estimator_model: string; estimator_prompt: string;
  backfill_days: string; update_interval_minutes: string;
};

export const formValues = (s: Settings): FormValues => ({
  gateway_url: s.gateway_url, admin_key: "", estimator_key: "", github_token: "",
  github_api_url: s.github_api_url, github_connection: s.github_connection, repos: s.repos.join("\n"), estimator_model: s.estimator_model,
  estimator_prompt: s.estimator_prompt, backfill_days: String(s.backfill_days),
  update_interval_minutes: String(s.update_interval_minutes),
});

export function settingsUpdate(values: FormValues, locked: string[], fields = Object.keys(values)) {
  return Object.fromEntries(fields.filter(name => !locked.includes(name)).map(name => {
    const value = values[name as keyof FormValues];
    if (name === "repos") return [name, value.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean)];
    if (name === "backfill_days" || name === "update_interval_minutes") return [name, Number(value)];
    return [name, value];
  }));
}

export function Field({ label, help, children, locked = false }: {
  label: string; help?: string; children: ReactNode; locked?: boolean;
}) {
  return <label className="field"><span>{label} {locked && <span className="muted text-xs font-normal">(managed by server)</span>}</span>
    {children}{help && <span className="field-help">{help}</span>}</label>;
}
