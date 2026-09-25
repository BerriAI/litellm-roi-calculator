import { useEffect, useState } from "react";
import { Github } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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
  const [pending, setPending] = useState(() => {
    const params = new URLSearchParams(location.search);
    return params.get("github") === "pending" || params.get("reason") === "approval";
  });
  const [error, setError] = useState(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("github") !== "error" || params.get("reason") === "approval") return "";
    return "GitHub connection was not completed. Click Connect GitHub to try again.";
  });
  const appMode = values.github_connection === "app";
  const source = JSON.stringify([appMode, installation, organization.trim(), values.github_api_url, values.github_token]);
  const current = source === loadedFrom ? result : null;
  const selected = values.repos.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean);
  const normalized = (repo: string) => repo.replace(/^https?:\/\/[^/]+\//i, "").replace(/\/$/, "").replace(/\.git$/, "").toLowerCase();
  const selectedNames = selected.map(normalized);
  const visibleRepos = current?.repos.filter(repo => repo.name.toLowerCase().includes(query.toLowerCase())) || [];

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

  async function connect(mode = "connect") {
    setBusy(true); onBusy(true); setError("");
    try {
      // Preserve repository choices before leaving this page; other unsaved form fields stay local.
      if (!locked("repos")) await api("/api/settings", "PUT", { repos: selected });
      const data = await api<{ url?: string; action?: string; manifest?: object; pending?: boolean }>("/api/github/connect", "POST", {
        return_to: location.search.includes("page=settings") || !!document.querySelector(".app-sidebar") ? "settings" : "setup",
        mode,
      });
      if (data.pending) { setPending(true); setBusy(false); onBusy(false); return; }
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
        <p className="field-help mt-1">{connection?.connected ? `Connected to ${connection.installations.map(item => item.account).join(", ")}.` : "Read-only access to your repositories."}</p>
      </div></div>
      <Button type="button" variant={connection?.connected ? "outline" : "default"} className="px-4" disabled={busy || !connection}
        onClick={() => void connect(pending ? "check" : connection?.connected ? "manage" : "connect")}>
        {busy && !connection?.connected ? "Connecting…" : pending ? "Check access" : connection?.connected ? "Manage access" : "Connect GitHub"}
      </Button>
    </div>
    {pending && <div className="rounded-md border p-4 text-sm" role="status">
      <p className="font-medium">Waiting for GitHub approval</p>
      <p className="mt-1 muted leading-relaxed">A company admin needs to approve repository access on GitHub. Once approved, click Check access to continue.</p>
      <Button type="button" variant="link" className="h-auto p-0 mt-3" disabled={busy} onClick={() => void connect("manage")}>Choose another GitHub account</Button>
    </div>}
    {connection && !connection.configured && <p className="field-help">GitHub will create a read-only App for this calculator. No keys or secrets to copy.</p>}
    {connection?.connected && !appMode && <Button type="button" variant="outline" className="w-fit" onClick={() => update("github_connection", "app")}>Use connected GitHub App</Button>}
    {appMode && connection && connection.installations.length > 1 && <Field label="GitHub account">
      <Select items={connection.installations.map(item => ({ value: item.id, label: item.account }))} value={installation} onValueChange={value => { if (value !== null) setInstallation(value); }} disabled={busy}>
        <SelectTrigger className="w-full" aria-label="GitHub account"><SelectValue /></SelectTrigger>
        <SelectContent className="p-1">{connection.installations.map(item => <SelectItem value={item.id} key={item.id}>{item.account}</SelectItem>)}</SelectContent>
      </Select>
    </Field>}
    {appMode && busy && !current && <p className="text-sm muted" role="status">Loading repositories…</p>}
    {current && <div className="repo-picker">
      <Input aria-label="Filter loaded repositories" placeholder="Find a repository…" value={query} onChange={e => setQuery(e.target.value)} />
      {visibleRepos.length > 0 && <div className="flex items-center justify-between gap-3 px-1 py-2 text-xs muted">
        <span>{selected.length} selected</span><div className="flex gap-3">
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
        {!visibleRepos.length && <p className="p-3 text-sm muted">{query ? "No repositories match your search." : "No repositories available. Use Manage access to choose repositories on GitHub."}</p>}
      </div>
      {current.has_more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void browse(true)}>Load more repositories</Button>}
    </div>}
    {selected.length > 0 && !current && <details className="details">
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
        <Field label="Repository names" locked={locked("repos")} help="One owner/repo or repository URL per line.">
          <Textarea name="repos" rows={3} value={values.repos} disabled={locked("repos")} onChange={e => update("repos", e.target.value)} placeholder="your-company/your-repo" />
        </Field>
      </div>
    </details>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
