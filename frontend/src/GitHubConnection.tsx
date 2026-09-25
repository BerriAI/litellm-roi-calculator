import { useEffect, useState } from "react";
import { Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type Settings } from "./api";
import { Field, type FormValues } from "./configuration";

type Repository = { name: string; visibility: string; archived: boolean };
type RepositoryPage = { repos: Repository[]; has_more: boolean };
type Connection = { configured: boolean; connected: boolean; installations: { id: number; account: string }[] };
export const githubConnectionFields = ["github_connection", "github_token", "github_api_url"];

export function GitHubConnection({ values, saved, locked, update, saveConnection, onBusy }: {
  values: FormValues; saved: Settings; locked: (name: keyof FormValues) => boolean;
  update: (name: keyof FormValues, value: string) => void;
  saveConnection: () => Promise<Settings>; onBusy: (busy: boolean) => void;
}) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [installation, setInstallation] = useState(0);
  const [organization, setOrganization] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RepositoryPage | null>(null);
  const [loadedFrom, setLoadedFrom] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("github") !== "error") return "";
    return params.get("reason") === "approval" ? "Your organization may need to approve access. Once approved, click Connect GitHub again."
      : "GitHub connection was not completed. Click Connect GitHub to try again.";
  });
  const appMode = values.github_connection === "app";
  const source = JSON.stringify([appMode, installation, organization.trim(), values.github_api_url, values.github_token]);
  const current = source === loadedFrom ? result : null;
  const selected = values.repos.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean);
  const normalized = (repo: string) => repo.replace(/^https?:\/\/[^/]+\//i, "").replace(/\/$/, "").replace(/\.git$/, "").toLowerCase();
  const selectedNames = selected.map(normalized);

  useEffect(() => { void api<Connection>("/api/github/app").then(data => {
    setConnection(data); setInstallation(data.installations[0]?.id || 0);
  }).catch(e => setError(errorMessage(e))); }, []);

  // Returning from GitHub opens the repository picker immediately.
  useEffect(() => {
    if (!appMode || !installation) return;
    let active = true;
    setBusy(true); setError("");
    void api<RepositoryPage>(`/api/github/repos?installation=${installation}`).then(data => {
      if (active) { setResult(data); setLoadedFrom(source); setPage(1); }
    }).catch(e => { if (active) setError(errorMessage(e)); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [appMode, installation, source]);

  async function connect() {
    setBusy(true); onBusy(true); setError("");
    try {
      // Preserve repository choices before leaving this page; other unsaved form fields stay local.
      await api("/api/settings", "PUT", { repos: selected });
      const data = await api<{ url?: string; action?: string; manifest?: object }>("/api/github/connect", "POST", {
        return_to: location.search.includes("page=settings") || !!document.querySelector(".app-sidebar") ? "settings" : "setup",
      });
      if (data.url) { location.assign(data.url); return; }
      const form = document.createElement("form");
      form.method = "POST"; form.action = data.action!;
      const input = document.createElement("input");
      input.type = "hidden"; input.name = "manifest"; input.value = JSON.stringify(data.manifest);
      form.append(input); document.body.append(form); form.submit();
    } catch (e) { setError(errorMessage(e)); setBusy(false); onBusy(false); }
  }

  async function browse(more = false) {
    setBusy(true); onBusy(true); setError("");
    try {
      const savedConnection = await saveConnection();
      const nextPage = more ? page + 1 : 1;
      const data = await api<RepositoryPage>(`/api/github/repos?org=${encodeURIComponent(organization.trim())}&installation=${appMode ? installation : 0}&page=${nextPage}`);
      setResult({ repos: more ? [...new Map([...(current?.repos || []), ...data.repos].map(repo => [repo.name, repo])).values()] : data.repos, has_more: data.has_more });
      setLoadedFrom(JSON.stringify([appMode, installation, organization.trim(), savedConnection.github_api_url, ""]));
      setPage(nextPage);
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); onBusy(false); }
  }

  function toggle(repo: string, checked: boolean) {
    if (checked && selected.length >= 50) { setError("Select up to 50 repositories."); return; }
    update("repos", (checked ? [...selected, repo] : selected.filter(value => normalized(value) !== repo.toLowerCase())).join("\n"));
    setError("");
  }

  return <div className="form-fields">
    <div className="github-connect">
      <div className="github-connect-identity"><span className="github-connect-icon"><Github aria-hidden="true" /></span><div>
        <p className="text-sm font-medium">GitHub</p>
        <p className="field-help mt-1">{connection?.connected ? `Connected to ${connection.installations.map(item => item.account).join(", ")}.` : "Read-only access to your repositories."}</p>
      </div></div>
      <Button type="button" variant={connection?.connected ? "outline" : "default"} className="px-4" disabled={busy} onClick={() => void connect()}>
        {connection?.connected ? "Manage access" : "Connect GitHub"}
      </Button>
    </div>
    {connection && !connection.configured && <p className="field-help">GitHub will guide you through creating an App for this workspace.</p>}
    {connection?.connected && !appMode && <Button type="button" variant="outline" className="w-fit" onClick={() => update("github_connection", "app")}>Use connected GitHub App</Button>}
    {appMode && connection && connection.installations.length > 1 && <Field label="GitHub account">
      <select className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={installation} onChange={e => setInstallation(Number(e.target.value))}>
        {connection.installations.map(item => <option value={item.id} key={item.id}>{item.account}</option>)}
      </select>
    </Field>}
    {appMode && busy && !current && <p className="text-sm muted" role="status">Loading repositories…</p>}
    {current && <div className="repo-picker">
      <Input aria-label="Filter loaded repositories" placeholder="Find a repository…" value={query} onChange={e => setQuery(e.target.value)} />
      <div className="repo-list" aria-label="Available repositories">
        {current.repos.filter(repo => repo.name.toLowerCase().includes(query.toLowerCase())).map(repo => <label key={repo.name} className="repo-choice">
          <input type="checkbox" checked={selectedNames.includes(repo.name.toLowerCase())} disabled={locked("repos")}
            onChange={e => toggle(repo.name, e.target.checked)} />
          <span className="min-w-0 flex-1 break-words">{repo.name}</span><span className="text-xs muted">{repo.visibility}{repo.archived ? " · archived" : ""}</span>
        </label>)}
        {!current.repos.some(repo => repo.name.toLowerCase().includes(query.toLowerCase())) && <p className="p-3 text-sm muted">No repositories found. Check repository access on GitHub.</p>}
      </div>
      {current.has_more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void browse(true)}>Load more repositories</Button>}
    </div>}
    {selected.length > 0 && <details className="details">
      <summary>{selected.length} {selected.length === 1 ? "repository" : "repositories"} selected</summary>
      <div className="flex flex-wrap gap-2">{selected.map(repo => <span key={repo} className="inline-flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
        {repo}<button type="button" disabled={locked("repos")} aria-label={`Remove ${repo}`} onClick={() => toggle(repo, false)}>×</button>
      </span>)}</div>
    </details>}
    <details className="details github-advanced" open={!appMode && (saved.has_github_token || values.github_api_url !== "https://api.github.com") || undefined}>
      <summary>Advanced connection settings</summary>
      <div className="form-fields pt-4">
        {appMode && <Button type="button" variant="outline" className="w-fit" onClick={() => update("github_connection", "token")}>Use manual connection</Button>}
        {!appMode && <>
          <Field label="GitHub token" locked={locked("github_token")} help="For private and internal repos, use read access to Pull requests, Contents, and Metadata. Organization approval or SSO authorization may be required.">
            <Input name="github_token" type="password" autoComplete="new-password" value={values.github_token} disabled={locked("github_token")}
              placeholder={saved.has_github_token ? "Saved. Leave blank to keep." : "github_pat_…"} onChange={e => update("github_token", e.target.value)} />
          </Field>
          <Field label="GitHub API URL" locked={locked("github_api_url")} help="For Enterprise Server, use https://github.company.com/api/v3.">
            <Input name="github_api_url" type="url" value={values.github_api_url} disabled={locked("github_api_url")} onChange={e => update("github_api_url", e.target.value)} required />
          </Field>
          <div className="flex items-end gap-3">
            <div className="min-w-0 flex-1"><Field label="Organization (optional)"><Input value={organization} onChange={e => setOrganization(e.target.value)} placeholder="your-company" /></Field></div>
            <Button type="button" variant="outline" disabled={busy} onClick={() => void browse()}>{busy ? "Loading…" : "Browse repos"}</Button>
          </div>
        </>}
        <Field label="Repository names" locked={locked("repos")} help="One owner/repo or repository URL per line. Up to 50 repositories.">
          <Textarea name="repos" rows={3} value={values.repos} disabled={locked("repos")} onChange={e => update("repos", e.target.value)} placeholder="your-company/your-repo" />
        </Field>
      </div>
    </details>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
