export interface Settings {
  gateway_url: string;
  github_api_url: string;
  repos: string[];
  estimator_model: string;
  estimator_prompt: string;
  identity_map: Record<string, string>;
  backfill_days: number;
  update_interval_minutes: number;
  has_admin_key: boolean;
  has_estimator_key: boolean;
  has_github_token: boolean;
  ready: boolean;
}

export interface Pull {
  repo: string;
  number: number;
  title: string;
  url: string;
  login: string;
  merged_at: string;
  email: string;
  matched: boolean;
  match_method: string;
  estimate: {
    status: string;
    hours?: number;
    reasoning?: string;
    error?: string;
    model?: string;
    cached?: boolean;
  };
}

export interface Person {
  id: string;
  email: string;
  logins: string[];
  spend: number | null;
  hours: number;
  prs: number;
  estimated_prs: number;
  pending_prs: number;
  eligible: boolean;
  match_methods: string[];
  cost_per_hour: number | null;
}

export interface Report {
  id: number | null;
  start: string;
  end: string;
  synced_at: string;
  repos: string[];
  estimator_model: string;
  estimator_prompt: string;
  warnings: string[];
  metrics: {
    output_hours: number;
    matched_spend: number;
    total_spend: number;
    excluded_spend: number;
    cost_per_hour: number | null;
    merged_prs: number;
    estimated_prs: number;
    matched_prs: number;
    pending_prs: number;
    cohort_people: number;
  };
  pulls: Pull[];
  people: Person[];
}

export interface AppState {
  settings: Settings;
  environment_fields: string[];
  default_prompt: string;
  report: Report | null;
  status: {
    running: boolean;
    stage: string;
    done: number;
    total: number;
    next_update: string | null;
    error: string | null;
  };
}

export async function api<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    ...(data !== undefined && { body: JSON.stringify(data) }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Check the settings and try again.");
  return result as T;
}

export const money = (value: number | null) => value == null ? "—" : new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 2,
}).format(value);
export const number = (value: number = 0) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(value);
export const date = (value: string) => new Date(value.slice(0, 10) + "T12:00:00Z").toLocaleDateString("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
});
export const safeURL = (value: string) => /^https?:\/\//i.test(value || "") ? value : "#";
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : "Something went wrong. Try again.";
