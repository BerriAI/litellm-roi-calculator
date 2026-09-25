import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, Check, Circle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TextPicker } from "./TextPicker";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type AppState, type Settings } from "./api";
import { Field, formValues, settingsUpdate, type FormValues } from "./configuration";
import { GitHubConnection, githubConnectionFields } from "./GitHubConnection";
import { SyncProgress } from "./SyncProgress";

const steps = ["Gateway", "Repositories", "Estimator & sync"];
const stepFields = [
  ["gateway_url", "admin_key"],
  [...githubConnectionFields, "repos"],
  ["estimator_model", "estimator_prompt", "estimator_key", "backfill_days", "update_interval_hours"],
];

export function Onboarding({ state, refresh, connectionError }: {
  state: AppState; refresh: () => Promise<void>; connectionError: string;
}) {
  const [screen, setScreen] = useState<"setup" | "progress">(
    state.status.running || ["error", "cancelled"].includes(state.status.phase) ? "progress" : "setup");
  const [step, setStep] = useState(new URLSearchParams(location.search).has("github") ? 1 : state.settings.repos.length ? 2 : state.settings.gateway_url && state.settings.has_admin_key ? 1 : 0);
  const [values, setValues] = useState(() => formValues(state.settings));
  const [saved, setSaved] = useState(state.settings);
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState(false);
  const [modelRetry, setModelRetry] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const locked = (name: keyof FormValues) => state.environment_fields.includes(name);
  const update = (name: keyof FormValues, value: string) => { setValues(v => ({ ...v, [name]: value })); setError(""); };
  const progress = state.status.running || screen === "progress";
  useEffect(() => { heading.current?.focus(); window.scrollTo(0, 0); }, [screen, step, progress]);
  useEffect(() => {
    if (step !== 2 || !saved.gateway_url || !saved.has_admin_key) return;
    let active = true;
    setModelsLoading(true); setModelsError(false);
    void api<{ models: string[] }>("/api/models").then(result => {
      if (active) setModels(result.models);
    }).catch(() => { if (active) setModelsError(true); })
      .finally(() => { if (active) setModelsLoading(false); });
    return () => { active = false; };
  }, [step, saved.gateway_url, saved.has_admin_key, modelRetry]);

  async function save(fields: string[]) {
    const result = await api<Settings>("/api/settings", "PUT", settingsUpdate(values, state.environment_fields, fields));
    setSaved(result);
    const normalized = formValues(result);
    setValues(v => ({ ...v, ...Object.fromEntries(fields.map(field => [field, normalized[field as keyof FormValues]])) }));
    await refresh();
    return result;
  }

  async function advance(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await save(stepFields[step]);
      if (step < 2) {
        const result = await api<{ models: string[] }>(`/api/connections/test?scope=${step === 0 ? "gateway" : "github"}`, "POST", {});
        if (step === 0) setModels(result.models);
        setStep(step + 1);
      } else {
        await api("/api/sync", "POST", {});
        setScreen("progress");
        await refresh();
      }
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  async function syncAction(cancel = false) {
    setBusy(true); setError("");
    try { await api("/api/sync", cancel ? "DELETE" : "POST", cancel ? undefined : {}); await refresh(); }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  const input = (name: keyof FormValues, type = "text", placeholder = "", required = false) => <Input
    name={name} type={type} value={values[name]} onChange={e => update(name, e.target.value)} required={required}
    disabled={locked(name)} placeholder={placeholder} autoComplete={type === "password" ? "new-password" : "off"} />;
  const phases = ["spend", "repositories", "estimates"];
  const activePhase = state.status.total > 0 ? 2 : phases.indexOf(state.status.phase);

  return <div className="onboarding">
    <header className="onboarding-brand"><a href="/" className="brand-link" aria-label="LiteLLM ROI Calculator home">
      <img src="/assets/litellm-icon.jpg" width={36} height={36} alt="" className="size-9 rounded-full" />
      <span className="flex flex-col gap-0.5"><span className="font-semibold tracking-tight">LiteLLM</span><span className="text-xs muted">ROI Calculator</span></span>
    </a></header>
    <main className="onboarding-content">
      {progress ? <section className="setup-panel">
        <h1 ref={heading} tabIndex={-1}>{state.status.running ? "Preparing your dashboard" : state.status.phase === "cancelled" ? "Backfill cancelled" : state.status.error ? "Backfill needs attention" : "Starting backfill"}</h1>
        <p className="onboarding-description">{state.status.running ? "Importing the last" : "History window:"} {state.settings.backfill_days} days from your gateway and {state.settings.repos.length} {state.settings.repos.length === 1 ? "repository" : "repositories"}.</p>
        <ol className="backfill-stages" aria-label="Backfill progress">{["Import gateway spend", "Import merged pull requests", "Estimate engineering hours"].map((label, i) => <li key={label} className={activePhase === i ? "active" : ""}>
          {activePhase > i ? <Check aria-label="Complete" /> : state.status.running && activePhase === i ? <Loader2 className="animate-spin" aria-label="In progress" /> : <Circle aria-label="Pending" />}
          <span>{label}</span>
        </li>)}</ol>
        <div className="backfill-detail"><SyncProgress status={state.status} /></div>
        {state.status.error && <p role="alert" className="text-sm text-destructive mt-5">{state.status.error}</p>}
        <div className="onboarding-actions">
          {state.status.running ? <Button variant="outline" disabled={busy} onClick={() => void syncAction(true)}>Cancel backfill</Button> : <>
            <Button disabled={busy} onClick={() => void syncAction()}>{busy ? "Starting…" : "Retry backfill"}</Button>
            <Button variant="outline" disabled={busy} onClick={() => { setScreen("setup"); setStep(0); setError(""); }}>Edit setup</Button>
          </>}
        </div>
        <p className="setup-footnote">The dashboard opens when backfill finishes. Keep the app running; you can reload this page.</p>
      </section> : <>
        <ol className="setup-steps" aria-label="Setup steps">{steps.map((label, i) => <li key={label} aria-current={step === i ? "step" : undefined}><span>{i < step ? <Check className="size-3.5" /> : i + 1}</span>{label}</li>)}</ol>
        <section className="setup-panel">
        <h1 ref={heading} tabIndex={-1}>{["Connect your gateway", "Choose your repositories", "Estimator & sync"][step]}</h1>
        <p className="onboarding-description">{["Read user spend from your LiteLLM gateway.", "Select the repositories you want to measure.", "Choose a model and how often to update your report."][step]}</p>
        <form onSubmit={e => void advance(e)}>
          <fieldset disabled={busy} className="form-fields">
            {step === 0 ? <>
              <Field label="Gateway URL" locked={locked("gateway_url")}>{input("gateway_url", "url", "http://localhost:4000", true)}</Field>
              <Field label="Admin key" locked={locked("admin_key")} help="Use an admin or read-only admin key with access to user spend.">
                {input("admin_key", "password", saved.has_admin_key ? "Saved. Leave blank to keep." : "sk-…", !saved.has_admin_key)}
              </Field>
            </> : step === 1 ? <GitHubConnection values={values} saved={saved} locked={locked} update={update}
              saveConnection={() => save(githubConnectionFields)} onBusy={setBusy} /> : <>
              <Field label="Estimator model" locked={locked("estimator_model")}>
                <TextPicker items={models} name="estimator_model" required value={values.estimator_model} disabled={locked("estimator_model")}
                  onChange={value => update("estimator_model", value)} placeholder="Choose or enter a model from your gateway" />
              </Field>
              {modelsLoading && <p className="field-help" role="status">Loading models from your gateway…</p>}
              {modelsError && <p className="field-help">Could not load models. Enter a model name or <button type="button" className="underline" onClick={() => setModelRetry(n => n + 1)}>retry</button>.</p>}
              <p className="field-help">We recommend a small model, such as GPT Luna or Claude Haiku.</p>
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Backfill (days)" help="Rolling history window, including today."><Input name="backfill_days" type="number" min={1} max={3650} required value={values.backfill_days} onChange={e => update("backfill_days", e.target.value)} /></Field>
                <Field label="Update interval (hours)" help="0 for manual updates."><Input name="update_interval_hours" type="number" min={0} max={720} step="any" required value={values.update_interval_hours} onChange={e => update("update_interval_hours", e.target.value)} /></Field>
              </div>
              <details className="details"><summary>Advanced options</summary><div className="form-fields pt-3">
                <Field label="Prompt" help="Temperature is fixed at 0.">
                  <Textarea name="estimator_prompt" rows={4} maxLength={20000} value={values.estimator_prompt} onChange={e => update("estimator_prompt", e.target.value)} />
                </Field>
                <Button type="button" variant="link" className="p-0 h-auto w-fit" onClick={() => update("estimator_prompt", state.default_prompt)}>Reset prompt</Button>
                <Field label="Separate estimator key" locked={locked("estimator_key")} help="Required if your admin key cannot make model calls. A separate service user keeps estimation costs out of people’s spend.">
                {input("estimator_key", "password", saved.has_estimator_key ? "Saved. Leave blank to keep." : "Uses your admin key")}
              </Field></div></details>
              <p className="field-help">PR descriptions and change statistics go to your model to estimate engineering hours without AI assistance, not actual time spent.</p>
            </>}
            <div className="onboarding-actions justify-between">
              {step > 0 ? <Button type="button" variant="ghost" className="px-4" onClick={() => { setStep(step - 1); setError(""); }}>Back</Button> : <span />}
              <Button type="submit" className="px-4" disabled={step === 1 && !values.repos.trim()}>{busy ? <><Loader2 className="animate-spin" />{step < 2 ? "Checking…" : "Starting…"}</> : step < 2 ? <>Continue<ArrowRight /></> : "Start backfill"}</Button>
            </div>
          </fieldset>
        </form>
        </section>
      </>}
      {(error || connectionError) && <p role="alert" className="text-sm text-destructive mt-5">{error || connectionError}</p>}
    </main>
  </div>;
}
