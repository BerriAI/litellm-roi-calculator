import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TextPicker } from "./TextPicker";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { api, errorMessage, type AppState, type Settings } from "./api";
import { GitHubConnection, githubConnectionFields } from "./GitHubConnection";
import { Field, formValues, settingsUpdate, type FormValues } from "./configuration";

export function SettingsForm({ state, refresh, onSync }: {
  state: AppState; refresh: () => Promise<void>; onSync: () => Promise<void>;
}) {
  const [values, setValues] = useState(() => formValues(state.settings));
  const [saved, setSaved] = useState(state.settings);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [resetOpen, setResetOpen] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const locked = (name: keyof FormValues) => state.environment_fields.includes(name);
  const update = (name: keyof FormValues, value: string) => { setValues(v => ({ ...v, [name]: value })); setMessage(null); };

  async function save(fields = Object.keys(values)) {
    const result = await api<Settings>("/api/settings", "PUT", settingsUpdate(values, state.environment_fields, fields));
    setSaved(result);
    const normalized = formValues(result);
    setValues(v => ({ ...v, ...Object.fromEntries(fields.map(field => [field, normalized[field as keyof FormValues]])) }));
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

  async function resetSetup() {
    setBusy("reset"); setMessage(null);
    try { await api("/api/setup/reset", "POST", {}); await refresh(); }
    catch (error) { setMessage({ text: errorMessage(error), error: true }); }
    finally { setBusy(""); setResetOpen(false); }
  }

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
        <GitHubConnection values={values} saved={saved} locked={locked} update={update}
          saveConnection={() => save(githubConnectionFields)} onBusy={value => setBusy(value ? "repos" : "")} />
      </section>
      <section className="form-section">
        <h2>Estimator</h2>
        <div className="form-fields">
          <div className="flex items-end gap-2">
            <div className="min-w-0 flex-1"><Field label="Model" locked={locked("estimator_model")}>
              <TextPicker items={models} name="estimator_model" value={values.estimator_model} disabled={locked("estimator_model")}
                onChange={value => update("estimator_model", value)} placeholder="Model name on your gateway" />
            </Field></div>
            <Button type="button" variant="outline" onClick={() => void action("models")}>{busy === "models" ? "Loading…" : "Load models"}</Button>
          </div>
          <p className="field-help">We recommend a small model, such as GPT Luna or Claude Haiku.</p>
          <Field label="Prompt" help="Estimates assume the work is completed without AI assistance. Temperature is fixed at 0. The model receives PR descriptions, file change counts, and commit metadata.">
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
          <Field label="Update interval (hours)" help="0 for manual updates.">
            <Input name="update_interval_hours" type="number" min={0} max={720} step="any" required value={values.update_interval_hours}
              onChange={e => update("update_interval_hours", e.target.value)} />
          </Field>
        </div>
        <p className="field-help mt-4">Automatic updates run while the local app is running.</p>
      </section>
      <details className="details form-section">
        <summary>Advanced settings</summary>
        <div className="form-fields pt-3">
          <Field label="Estimator API key" locked={locked("estimator_key")} help="Optional dedicated inference key. Otherwise uses the admin key. A separate service user keeps estimation costs out of people's spend.">
            {input("estimator_key", "password", saved.has_estimator_key ? "Saved. Leave blank to keep." : "sk-…")}
          </Field>
          <Button type="button" variant="outline" className="w-fit" onClick={() => setResetOpen(true)}>Restart setup</Button>
        </div>
      </details>
      <div className="flex flex-wrap gap-2 py-5">
        <Button type="submit">{busy === "save" ? "Saving…" : "Save settings"}</Button>
        <Button type="button" variant="outline" onClick={() => void action("test")}>{busy === "test" ? "Testing…" : "Test connections"}</Button>
        <Button type="button" variant="outline" onClick={() => void action("sync")}>{busy === "sync" ? "Starting…" : "Save & sync"}</Button>
      </div>
    </fieldset>
    {message && <p role={message.error ? "alert" : "status"} className={`text-sm pb-5 ${message.error ? "text-destructive" : "muted"}`}>{message.text}</p>}
    <Dialog open={resetOpen} onOpenChange={setResetOpen}>
      <DialogContent>
        <DialogHeader><DialogTitle>Restart setup?</DialogTitle>
          <DialogDescription>Clear reports and repository selections, then return to setup. Saved gateway and GitHub connections stay connected. Cached estimates can be reused.</DialogDescription></DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" disabled={!!busy} onClick={() => setResetOpen(false)}>Cancel</Button>
          <Button type="button" disabled={!!busy} onClick={() => void resetSetup()}>{busy === "reset" ? "Resetting…" : "Restart setup"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </form>;
}
