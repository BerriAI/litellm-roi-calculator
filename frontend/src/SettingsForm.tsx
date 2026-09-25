import { useState, type FormEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type AppState, type Settings } from "./api";

type FormValues = {
  gateway_url: string; admin_key: string; estimator_key: string; github_token: string;
  github_api_url: string; repos: string; estimator_model: string; estimator_prompt: string;
  backfill_days: string; update_interval_minutes: string;
};

const formValues = (s: Settings): FormValues => ({
  gateway_url: s.gateway_url, admin_key: "", estimator_key: "", github_token: "",
  github_api_url: s.github_api_url, repos: s.repos.join("\n"), estimator_model: s.estimator_model,
  estimator_prompt: s.estimator_prompt, backfill_days: String(s.backfill_days),
  update_interval_minutes: String(s.update_interval_minutes),
});

function Field({ label, help, children, locked = false }: {
  label: string; help?: string; children: ReactNode; locked?: boolean;
}) {
  return <label className="field"><span>{label} {locked && <span className="muted text-xs font-normal">(set by environment)</span>}</span>
    {children}{help && <span className="field-help">{help}</span>}</label>;
}

export function SettingsForm({ state, refresh, onSync }: {
  state: AppState; refresh: () => Promise<void>; onSync: () => Promise<void>;
}) {
  const [values, setValues] = useState(() => formValues(state.settings));
  const [saved, setSaved] = useState(state.settings);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const locked = (name: keyof FormValues) => state.environment_fields.includes(name);
  const update = (name: keyof FormValues, value: string) => { setValues(v => ({ ...v, [name]: value })); setMessage(null); };

  async function save() {
    const data = Object.fromEntries(Object.entries(values).filter(([key]) => !state.environment_fields.includes(key)));
    const result = await api<Settings>("/api/settings", "PUT", {
      ...data,
      ...(data.repos !== undefined && { repos: data.repos.split(/[\n,]/).map(r => r.trim()).filter(Boolean) }),
      backfill_days: Number(values.backfill_days), update_interval_minutes: Number(values.update_interval_minutes),
    });
    setSaved(result);
    setValues(formValues(result));
    await refresh();
    return result;
  }

  async function action(kind: "save" | "models" | "test" | "sync") {
    setBusy(kind); setMessage(null);
    try {
      await save();
      if (kind === "models") {
        const result = await api<{ models: string[] }>("/api/models");
        setModels(result.models);
        setMessage({ text: result.models.length ? "Models loaded. Select or enter a model below." : "No models returned. Enter a model name manually.", error: false });
      } else if (kind === "test") {
        const result = await api<{ models: string[]; repos: number }>("/api/connections/test", "POST", {});
        setModels(result.models);
        setMessage({ text: `Gateway and ${result.repos} ${result.repos === 1 ? "repository" : "repositories"} connected.`, error: false });
      } else if (kind === "sync") {
        await onSync();
      } else {
        setMessage({ text: "Settings saved. Changes apply on the next sync.", error: false });
      }
    } catch (error) { setMessage({ text: errorMessage(error), error: true }); }
    finally { setBusy(""); }
  }

  const input = (name: keyof FormValues, type = "text", placeholder = "") => <Input
    name={name} type={type} value={values[name]} onChange={e => update(name, e.target.value)}
    disabled={locked(name)} placeholder={placeholder} autoComplete={type === "password" ? "new-password" : "off"}
  />;
  const submit = (event: FormEvent) => { event.preventDefault(); void action("save"); };

  return <form onSubmit={submit} className="max-w-3xl">
    <fieldset disabled={!!busy || state.status.running}>
      <section className="form-section">
        <h2>LiteLLM gateway</h2>
        <div className="form-fields">
          <Field label="Gateway URL" locked={locked("gateway_url")}>{input("gateway_url", "url", "http://localhost:4000")}</Field>
          <Field label="Admin key" locked={locked("admin_key")} help="Admin or read-only admin key with access to user spend.">
            {input("admin_key", "password", saved.has_admin_key ? "Saved. Leave blank to keep." : "sk-…")}
          </Field>
        </div>
      </section>
      <section className="form-section">
        <h2>GitHub</h2>
        <div className="form-fields">
          <Field label="Repositories" locked={locked("repos")} help="One owner/repository or GitHub repository URL per line.">
            <Textarea name="repos" rows={3} className="min-h-24" value={values.repos} disabled={locked("repos")}
              onChange={e => update("repos", e.target.value)} placeholder={"BerriAI/litellm\nyour-org/your-repo"} />
          </Field>
          <Field label="GitHub token" locked={locked("github_token")} help="Required for private repos. Give read access to Pull requests, Contents, and Metadata.">
            {input("github_token", "password", saved.has_github_token ? "Saved. Leave blank to keep." : "github_pat_…")}
          </Field>
        </div>
      </section>
      <section className="form-section">
        <h2>Estimator</h2>
        <div className="form-fields">
          <div className="flex items-end gap-2">
            <div className="min-w-0 flex-1"><Field label="Model" locked={locked("estimator_model")}>
              <Input name="estimator_model" list="gateway-models" value={values.estimator_model} disabled={locked("estimator_model")}
                onChange={e => update("estimator_model", e.target.value)} placeholder="Model name on your gateway" />
              <datalist id="gateway-models">{models.map(model => <option key={model} value={model} />)}</datalist>
            </Field></div>
            <Button type="button" variant="outline" onClick={() => void action("models")}>{busy === "models" ? "Loading…" : "Load models"}</Button>
          </div>
          <Field label="Prompt" help="Temperature is fixed at 0. PR titles, descriptions, and diffs are sent to this model.">
            <Textarea name="estimator_prompt" rows={4} className="min-h-28" value={values.estimator_prompt}
              onChange={e => update("estimator_prompt", e.target.value)} required maxLength={20000} />
          </Field>
          <Button type="button" variant="link" size="sm" className="w-fit p-0" onClick={() => update("estimator_prompt", state.default_prompt)}>Reset prompt</Button>
        </div>
      </section>
      <section className="form-section">
        <h2>Sync</h2>
        <div className="grid gap-5 sm:grid-cols-2">
          <Field label="Backfill (days)" help="Rolling history window. Applied on the next sync.">
            <Input name="backfill_days" type="number" min={1} max={3650} required value={values.backfill_days}
              onChange={e => update("backfill_days", e.target.value)} />
          </Field>
          <Field label="Update interval (minutes)" help="0 for manual updates, or at least 5 minutes.">
            <Input name="update_interval_minutes" type="number" min={0} max={43200} required value={values.update_interval_minutes}
              onChange={e => update("update_interval_minutes", e.target.value)} />
          </Field>
        </div>
        <p className="field-help mt-4">Automatic updates run while this app is open.</p>
      </section>
      <details className="details form-section">
        <summary>Advanced settings</summary>
        <div className="form-fields pt-3">
          <Field label="Estimator API key" locked={locked("estimator_key")} help="Optional dedicated inference key. Otherwise uses the admin key. A separate service user keeps estimation costs out of people's spend.">
            {input("estimator_key", "password", saved.has_estimator_key ? "Saved. Leave blank to keep." : "sk-…")}
          </Field>
          <Field label="GitHub API URL" locked={locked("github_api_url")} help="Change this for GitHub Enterprise.">{input("github_api_url", "url")}</Field>
        </div>
      </details>
      <div className="flex flex-wrap gap-2 py-5">
        <Button type="submit">{busy === "save" ? "Saving…" : "Save settings"}</Button>
        <Button type="button" variant="outline" onClick={() => void action("test")}>{busy === "test" ? "Testing…" : "Test connections"}</Button>
        <Button type="button" variant="outline" onClick={() => void action("sync")}>{busy === "sync" ? "Starting…" : "Save & sync"}</Button>
      </div>
    </fieldset>
    {message && <p role={message.error ? "alert" : "status"} className={`text-sm pb-5 ${message.error ? "text-destructive" : "muted"}`}>{message.text}</p>}
  </form>;
}
