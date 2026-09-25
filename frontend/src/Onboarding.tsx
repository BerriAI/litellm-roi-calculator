import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, Check, Circle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type AppState, type Settings } from "./api";
import { Field, formValues, settingsUpdate, type FormValues } from "./configuration";
import { GitHubConnection, githubConnectionFields } from "./GitHubConnection";

const steps = ["Gateway", "Repositories", "Estimator & sync"];
const stepFields = [
  ["gateway_url", "admin_key"],
  [...githubConnectionFields, "repos"],
  ["estimator_model", "estimator_prompt", "estimator_key", "backfill_days", "update_interval_minutes"],
];

export function Onboarding({ state, refresh, connectionError }: {
  state: AppState; refresh: () => Promise<void>; connectionError: string;
}) {
  const [screen, setScreen] = useState<"welcome" | "setup" | "progress">(
    state.status.running || ["error", "cancelled"].includes(state.status.phase) ? "progress" : "welcome");
  const [step, setStep] = useState(state.settings.repos.length ? 2 : state.settings.gateway_url && state.settings.has_admin_key ? 1 : 0);
  const [values, setValues] = useState(() => formValues(state.settings));
  const [saved, setSaved] = useState(state.settings);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  const locked = (name: keyof FormValues) => state.environment_fields.includes(name);
  const update = (name: keyof FormValues, value: string) => { setValues(v => ({ ...v, [name]: value })); setError(""); };
  const progress = state.status.running || screen === "progress";
  useEffect(() => { heading.current?.focus(); window.scrollTo(0, 0); }, [screen, step, progress]);

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

  async function loadModels() {
    setBusy(true); setError("");
    try { setModels((await api<{ models: string[] }>("/api/models")).models); }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  const input = (name: keyof FormValues, type = "text", placeholder = "", required = false) => <Input
    name={name} type={type} value={values[name]} onChange={e => update(name, e.target.value)} required={required}
    disabled={locked(name)} placeholder={placeholder} autoComplete={type === "password" ? "new-password" : "off"} />;
  const hasSetup = !!(saved.gateway_url || saved.repos.length);
  const phases = ["spend", "repositories", "estimates"];
  const activePhase = phases.indexOf(state.status.phase);

  return <div className="onboarding">
    <header className="onboarding-brand"><a href="/" className="brand-link" aria-label="LiteLLM ROI Calculator home">
      <img src="/assets/litellm-icon.jpg" width={36} height={36} alt="" className="size-9 rounded-full" />
      <span className="flex flex-col gap-0.5"><span className="font-semibold tracking-tight">LiteLLM</span><span className="text-xs muted">ROI Calculator</span></span>
    </a></header>
    <main className={`onboarding-content ${screen === "welcome" && !progress ? "onboarding-welcome" : ""}`}>
      {!progress && screen === "welcome" ? <>
        <h1 ref={heading} tabIndex={-1}>Set up your data</h1>
        <p className="onboarding-description">Connect your LiteLLM gateway and GitHub repositories to compare AI spend with estimated engineering hours.</p>
        <ol className="welcome-steps">{["Connect your gateway", "Choose your repositories", "Choose an estimator and start backfill"].map((label, i) => <li key={label}><span>{i + 1}</span>{label}</li>)}</ol>
        <Button onClick={() => setScreen("setup")}>{hasSetup ? "Continue setup" : "Get started"}<ArrowRight /></Button>
        <p className="mt-6 text-sm muted">Engineering hours are model estimates, not actual time spent.</p>
      </> : progress ? <>
        <h1 ref={heading} tabIndex={-1}>{state.status.running ? "Preparing your dashboard" : state.status.phase === "cancelled" ? "Backfill cancelled" : state.status.error ? "Backfill needs attention" : "Starting backfill"}</h1>
        <p className="onboarding-description">Importing the last {saved.backfill_days} days from your gateway and {saved.repos.length} {saved.repos.length === 1 ? "repository" : "repositories"}.</p>
        <ol className="backfill-stages" aria-label="Backfill progress">{["Import gateway spend", "Import merged pull requests", "Estimate engineering hours"].map((label, i) => <li key={label} className={activePhase === i ? "active" : ""}>
          {activePhase > i ? <Check aria-label="Complete" /> : state.status.running && activePhase === i ? <Loader2 className="animate-spin" aria-label="In progress" /> : <Circle aria-label="Pending" />}
          <span>{label}</span>
        </li>)}</ol>
        <div className="backfill-detail" role="status" aria-live="polite">
          <p>{state.status.stage === "idle" ? "Starting…" : state.status.stage}</p>
          {state.status.total > 0 && <>
            <progress max={state.status.total} value={state.status.done} aria-label="Pull requests processed" />
            <p className="muted">{state.status.done} of {state.status.total} PRs processed · {state.status.estimated} estimated{state.status.needs_attention > 0 && ` · ${state.status.needs_attention} need attention`}</p>
          </>}
        </div>
        {state.status.error && <p role="alert" className="text-sm text-destructive mt-5">{state.status.error}</p>}
        <div className="onboarding-actions">
          {state.status.running ? <Button variant="outline" disabled={busy} onClick={() => void syncAction(true)}>Cancel backfill</Button> : <>
            <Button disabled={busy} onClick={() => void syncAction()}>{busy ? "Starting…" : "Retry backfill"}</Button>
            <Button variant="outline" disabled={busy} onClick={() => { setScreen("setup"); setStep(0); setError(""); }}>Edit setup</Button>
          </>}
        </div>
        <p className="mt-5 text-sm muted">The dashboard opens when backfill finishes. Keep the local app running; you can reload this page.</p>
      </> : <>
        <ol className="setup-steps" aria-label="Setup steps">{steps.map((label, i) => <li key={label} aria-current={step === i ? "step" : undefined}><span>{i < step ? <Check className="size-3.5" /> : i + 1}</span>{label}</li>)}</ol>
        <h1 ref={heading} tabIndex={-1}>{["Connect your gateway", "Choose your repositories", "Estimator & sync"][step]}</h1>
        <p className="onboarding-description">{["Read user spend from your LiteLLM gateway.", "Connect GitHub or your company’s GitHub Enterprise server.", "Choose how to estimate engineering hours and keep your report up to date."][step]}</p>
        <form onSubmit={e => void advance(e)}>
          <fieldset disabled={busy} className="form-fields">
            {step === 0 ? <>
              <Field label="Gateway URL" locked={locked("gateway_url")}>{input("gateway_url", "url", "http://localhost:4000", true)}</Field>
              <Field label="Admin key" locked={locked("admin_key")} help="Use an admin or read-only admin key with access to user spend.">
                {input("admin_key", "password", saved.has_admin_key ? "Saved. Leave blank to keep." : "sk-…", !saved.has_admin_key)}
              </Field>
            </> : step === 1 ? <GitHubConnection values={values} saved={saved} locked={locked} update={update}
              saveConnection={() => save(githubConnectionFields)} onBusy={setBusy} /> : <>
              <div className="flex items-end gap-3"><div className="min-w-0 flex-1"><Field label="Estimator model" locked={locked("estimator_model")}>
                <Input name="estimator_model" required list="setup-models" value={values.estimator_model} disabled={locked("estimator_model")}
                  onChange={e => update("estimator_model", e.target.value)} placeholder="Model name on your gateway" />
                <datalist id="setup-models">{models.map(model => <option key={model} value={model} />)}</datalist>
              </Field></div><Button type="button" variant="outline" onClick={() => void loadModels()}>Load models</Button></div>
              <details className="details"><summary>Estimator prompt</summary><div className="pt-2"><Field label="Prompt">
                <Textarea name="estimator_prompt" rows={4} maxLength={20000} value={values.estimator_prompt} onChange={e => update("estimator_prompt", e.target.value)} />
              </Field><Button type="button" variant="link" className="p-0 mt-2 h-auto" onClick={() => update("estimator_prompt", state.default_prompt)}>Reset prompt</Button></div></details>
              <p className="text-sm muted leading-relaxed">PR titles, descriptions, and diffs are sent to this model through your gateway. Temperature is fixed at 0. Estimates are not actual time spent.</p>
              <div className="grid gap-5 sm:grid-cols-2">
                <Field label="Backfill (days)" help="Rolling history window, including today."><Input name="backfill_days" type="number" min={1} max={3650} required value={values.backfill_days} onChange={e => update("backfill_days", e.target.value)} /></Field>
                <Field label="Update interval (minutes)" help="0 for manual, or at least 5 minutes."><Input name="update_interval_minutes" type="number" min={0} max={43200} required value={values.update_interval_minutes} onChange={e => update("update_interval_minutes", e.target.value)} /></Field>
              </div>
              <details className="details"><summary>Separate estimator key (optional)</summary><div className="pt-2"><Field label="Estimator API key" locked={locked("estimator_key")} help="Required if your admin key cannot make model calls. A separate service user keeps estimation costs out of people’s spend.">
                {input("estimator_key", "password", saved.has_estimator_key ? "Saved. Leave blank to keep." : "Uses your admin key")}
              </Field></div></details>
              <p className="field-help">Automatic updates begin after the first backfill and run while the local app is running. You can change these options in Settings.</p>
            </>}
            <div className="onboarding-actions justify-between">
              <Button type="button" variant="outline" onClick={() => { if (step === 0) setScreen("welcome"); else setStep(step - 1); setError(""); }}>Back</Button>
              <Button type="submit">{busy ? <><Loader2 className="animate-spin" />{step < 2 ? "Checking…" : "Starting…"}</> : step < 2 ? <>Continue<ArrowRight /></> : "Start backfill"}</Button>
            </div>
          </fieldset>
        </form>
      </>}
      {(error || connectionError) && <p role="alert" className="text-sm text-destructive mt-5">{error || connectionError}</p>}
    </main>
  </div>;
}
