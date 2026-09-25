import { useEffect, useState } from "react";
import { Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type Settings } from "./api";
import { Field, type FormValues } from "./configuration";

type Repository = { name: string; visibility: string; archived: boolean };
type RepositoryPage = { repos: Repository[]; has_more: boolean };
type Connection = { configured: boolean; connected: boolean; account: string; callback_url: string };
type LegacyConnection = { installations: { id: number; account: string }[] };
export const githubConnectionFields = ["github_connection", "github_token", "github_api_url"];

export function GitHubConnection({ values, saved, locked, update, saveConnection, onBusy }: {
  values: FormValues; saved: Settings; locked: (name: keyof FormValues) => boolean;
  update: (name: keyof FormValues, value: string) => void;
  saveConnection: () => Promise<Settings>; onBusy: (busy: boolean) => void;
}) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [legacy, setLegacy] = useState<LegacyConnection | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const [clientID, setClientID] = useState("");
  const [clientSecret, setClientSecret] = useState("");
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
    return "GitHub connection was not completed. Click Connect GitHub to try again.";
  });
  const appMode = values.github_connection === "app";
  const oauthMode = values.github_connection === "oauth";
  const source = JSON.stringify([values.github_connection, installation, organization.trim(), values.github_api_url, values.github_token]);
  const current = source === loadedFrom ? result : null;
  const selected = values.repos.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean);
  const normalized = (repo: string) => repo.replace(/^https?:\/\/[^/]+\//i, "").replace(/\/$/, "").replace(/\.git$/, "").toLowerCase();
  const selectedNames = selected.map(normalized);
  const visibleRepos = current?.repos.filter(repo => repo.name.toLowerCase().includes(query.toLowerCase())) || [];

  useEffect(() => { void api<Connection>("/api/github/oauth").then(setConnection).catch(e => setError(errorMessage(e))); }, []);
  useEffect(() => {
    if (!appMode) return;
    void api<LegacyConnection>("/api/github/app").then(data => {
      setLegacy(data); setInstallation(data.installations[0]?.id || 0);
    }).catch(e => setError(errorMessage(e)));
  }, [appMode]);

  // Returning from GitHub opens the repository picker immediately.
  useEffect(() => {
    if (!(oauthMode && connection?.connected) && !(appMode && installation)) return;
    let active = true;
    setBusy(true); setError("");
    void api<RepositoryPage>(`/api/github/repos?installation=${appMode ? installation : 0}`).then(data => {
      if (active) { setResult(data); setLoadedFrom(source); setPage(1); }
    }).catch(e => { if (active) setError(errorMessage(e)); }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [appMode, oauthMode, connection?.connected, installation, source]);

  async function connect(configure = false) {
    if (!connection) return;
    if (!connection.configured && !configure) { setShowSetup(true); return; }
    setBusy(true); onBusy(true); setError("");
    try {
      if (configure) {
        await api("/api/github/oauth/setup", "POST", { client_id: clientID, client_secret: clientSecret });
        setClientSecret("");
        setConnection(await api<Connection>("/api/github/oauth"));
        setShowSetup(false);
      }
      // Preserve repository choices before leaving this page; other unsaved form fields stay local.
      if (!locked("repos")) await api("/api/settings", "PUT", { repos: selected });
      const data = await api<{ url: string }>("/api/github/oauth/connect", "POST", {
        return_to: location.search.includes("page=settings") || !!document.querySelector(".app-sidebar") ? "settings" : "setup",
      });
      location.assign(data.url);
    } catch (e) { setError(errorMessage(e)); setBusy(false); onBusy(false); }
  }

  async function browse(more = false) {
    setBusy(true); onBusy(true); setError("");
    try {
      const savedConnection = await saveConnection();
      const nextPage = more ? page + 1 : 1;
      const data = await api<RepositoryPage>(`/api/github/repos?org=${encodeURIComponent(organization.trim())}&installation=${appMode ? installation : 0}&page=${nextPage}`);
      setResult({ repos: more ? [...new Map([...(current?.repos || []), ...data.repos].map(repo => [repo.name, repo])).values()] : data.repos, has_more: data.has_more });
      setLoadedFrom(JSON.stringify([values.github_connection, installation, organization.trim(), savedConnection.github_api_url, ""]));
      setPage(nextPage);
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); onBusy(false); }
  }

  function toggle(repo: string, checked: boolean) {
    update("repos", (checked ? [...selected, repo] : selected.filter(value => normalized(value) !== repo.toLowerCase())).join("\n"));
    setError("");
  }

  function selectVisible(checked: boolean) {
    const names = new Set(visibleRepos.map(repo => repo.name.toLowerCase()));
    update("repos", (checked
      ? [...selected, ...visibleRepos.filter(repo => !selectedNames.includes(repo.name.toLowerCase())).map(repo => repo.name)]
      : selected.filter(repo => !names.has(normalized(repo)))).join("\n"));
  }

  return <div className="form-fields">
    <div className="github-connect">
      <div className="github-connect-identity"><span className="github-connect-icon"><Github aria-hidden="true" /></span><div>
        <p className="text-sm font-medium">GitHub</p>
        <p className="field-help mt-1">{oauthMode && connection?.connected ? `Connected as ${connection.account}.` : appMode && legacy?.installations.length ? `Connected to ${legacy.installations.map(item => item.account).join(", ")}.` : "Connect your account, then choose repositories."}</p>
      </div></div>
      <Button type="button" variant={oauthMode && connection?.connected ? "outline" : "default"} className="px-4" disabled={busy || !connection} onClick={() => void connect()}>
        {busy && !current ? "Connecting…" : oauthMode && connection?.connected ? "Reconnect" : "Connect GitHub"}
      </Button>
    </div>
    {showSetup && connection && !connection.configured && <div className="rounded-md border p-5 form-fields">
      <div><p className="text-sm font-medium">Enable GitHub OAuth</p>
        <p className="field-help mt-2">The host sets this up once. Afterward, anyone using this calculator can connect their GitHub account.</p></div>
      <p className="text-sm leading-relaxed"><a href="https://github.com/settings/applications/new" target="_blank" rel="noopener noreferrer" className="underline">Create a GitHub OAuth app</a> using this site's URL as the homepage and the callback below.</p>
      <Field label="Authorization callback URL"><Input value={connection.callback_url} readOnly onFocus={e => e.target.select()} /></Field>
      <Field label="Client ID"><Input value={clientID} autoComplete="off" onChange={e => setClientID(e.target.value)} /></Field>
      <Field label="Client secret"><Input type="password" value={clientSecret} autoComplete="new-password" onChange={e => setClientSecret(e.target.value)} /></Field>
      <div className="flex gap-2">
        <Button type="button" disabled={busy || !clientID.trim() || !clientSecret.trim()} onClick={() => void connect(true)}>Save & connect</Button>
        <Button type="button" variant="ghost" onClick={() => setShowSetup(false)}>Cancel</Button>
      </div>
    </div>}
    {appMode && legacy && legacy.installations.length > 1 && <Field label="GitHub account">
      <select className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={installation} onChange={e => setInstallation(Number(e.target.value))}>
        {legacy.installations.map(item => <option value={item.id} key={item.id}>{item.account}</option>)}
      </select>
    </Field>}
    {(appMode || oauthMode) && busy && !current && <p className="text-sm muted" role="status">Loading repositories…</p>}
    {current && <div className="repo-picker">
      <Input aria-label="Filter loaded repositories" placeholder="Find a repository…" value={query} onChange={e => setQuery(e.target.value)} />
      {visibleRepos.length > 0 && <div className="flex items-center justify-between gap-3 px-1 py-2 text-xs muted">
        <span>{selected.length} selected</span>
        <div className="flex gap-3">
          <button type="button" disabled={locked("repos")} onClick={() => selectVisible(true)}>Select shown</button>
          <button type="button" disabled={locked("repos")} onClick={() => selectVisible(false)}>Clear shown</button>
        </div>
      </div>}
      <div className="repo-list" aria-label="Available repositories">
        {visibleRepos.map(repo => <label key={repo.name} className="repo-choice">
          <input type="checkbox" checked={selectedNames.includes(repo.name.toLowerCase())} disabled={locked("repos")}
            onChange={e => toggle(repo.name, e.target.checked)} />
          <span className="min-w-0 flex-1 break-words">{repo.name}</span><span className="text-xs muted">{repo.visibility}{repo.archived ? " · archived" : ""}</span>
        </label>)}
        {!visibleRepos.length && <p className="p-3 text-sm muted">{query ? "No repositories match your search." : "No repositories available to this connection."}</p>}
      </div>
      {current.has_more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void browse(true)}>Load more repositories</Button>}
      {oauthMode && <details className="details text-xs"><summary>Missing a company repository?</summary>
        <p className="mt-2">If your organization restricts OAuth apps, an owner must approve this connection in GitHub. Reconnect after approval.</p>
      </details>}
    </div>}
    {selected.length > 0 && !current && <details className="details">
      <summary>{selected.length} {selected.length === 1 ? "repository" : "repositories"} selected</summary>
      <div className="flex flex-wrap gap-2">{selected.map(repo => <span key={repo} className="inline-flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
        {repo}<button type="button" disabled={locked("repos")} aria-label={`Remove ${repo}`} onClick={() => toggle(repo, false)}>×</button>
      </span>)}</div>
    </details>}
    <details className="details github-advanced" open={!appMode && !oauthMode && (saved.has_github_token || values.github_api_url !== "https://api.github.com") || undefined}>
      <summary>Advanced connection settings</summary>
      <div className="form-fields pt-4">
        {(appMode || oauthMode) && <Button type="button" variant="outline" className="w-fit" onClick={() => update("github_connection", "token")}>Use manual connection</Button>}
        {!appMode && !oauthMode && <>
          <Field label="GitHub token" locked={locked("github_token")} help="Optional for public repos. For private repos, use read access to Pull requests, Contents, and Metadata.">
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
        <Field label="Repository names" locked={locked("repos")} help="One owner/repo or repository URL per line.">
          <Textarea name="repos" rows={3} value={values.repos} disabled={locked("repos")} onChange={e => update("repos", e.target.value)} placeholder="your-company/your-repo" />
        </Field>
      </div>
    </details>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
