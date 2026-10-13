import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type Settings } from "./api";
import { Field, type FormValues } from "./configuration";

type Repository = { name: string; visibility: string; archived: boolean };
type RepositoryPage = { repos: Repository[]; has_more: boolean };
export const azureDevOpsConnectionFields = ["git_provider", "azure_devops_org", "azure_devops_pat"];

export function AzureDevOpsConnection({ values, saved, locked, update, saveConnection, onBusy }: {
  values: FormValues; saved: Settings; locked: (name: keyof FormValues) => boolean;
  update: (name: keyof FormValues, value: string) => void;
  saveConnection: () => Promise<Settings>; onBusy: (busy: boolean) => void;
}) {
  const [project, setProject] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RepositoryPage | null>(null);
  const [loadedFrom, setLoadedFrom] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const source = JSON.stringify([project.trim(), values.azure_devops_org, values.azure_devops_pat]);
  const current = source === loadedFrom ? result : null;
  const selected = values.repos.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean);
  const selectedNames = selected.map(repo => repo.toLowerCase());
  const visibleRepos = current?.repos.filter(repo => repo.name.toLowerCase().includes(query.toLowerCase())) || [];

  async function browse(more = false) {
    setBusy(true); onBusy(true); setError("");
    try {
      const savedConnection = await saveConnection();
      const nextPage = more ? page + 1 : 1;
      const data = await api<RepositoryPage>(`/api/azure/repos?project=${encodeURIComponent(project.trim())}&page=${nextPage}`);
      setResult({ repos: more ? [...new Map([...(current?.repos || []), ...data.repos].map(repo => [repo.name, repo])).values()] : data.repos, has_more: data.has_more });
      setLoadedFrom(JSON.stringify([project.trim(), savedConnection.azure_devops_org, ""]));
      setPage(nextPage);
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); onBusy(false); }
  }

  function toggle(repo: string, checked: boolean) {
    update("repos", (checked ? [...selected, repo] : selected.filter(value => value.toLowerCase() !== repo.toLowerCase())).join("\n"));
    setError("");
  }

  function selectVisible(checked: boolean) {
    const names = new Set(visibleRepos.map(repo => repo.name.toLowerCase()));
    update("repos", (checked
      ? [...selected, ...visibleRepos.filter(repo => !selectedNames.includes(repo.name.toLowerCase())).map(repo => repo.name)]
      : selected.filter(repo => !names.has(repo.toLowerCase()))).join("\n"));
  }

  return <div className="form-fields">
    <Field label="Azure DevOps organization" locked={locked("azure_devops_org")} help="The organization segment of https://dev.azure.com/organization.">
      <Input name="azure_devops_org" value={values.azure_devops_org} disabled={locked("azure_devops_org")}
        placeholder="your-organization" autoComplete="off" onChange={e => update("azure_devops_org", e.target.value)} required />
    </Field>
    <Field label="Personal access token" locked={locked("azure_devops_pat")} help="Needs the Code (Read) scope for the projects you select.">
      <Input name="azure_devops_pat" type="password" autoComplete="new-password" value={values.azure_devops_pat} disabled={locked("azure_devops_pat")}
        placeholder={saved.has_azure_devops_pat ? "Saved. Leave blank to keep." : "PAT with Code (Read)"} onChange={e => update("azure_devops_pat", e.target.value)} />
    </Field>
    <div className="flex items-end gap-3">
      <div className="min-w-0 flex-1"><Field label="Project (optional)" help="Leave blank to list repositories from all projects.">
        <Input value={project} onChange={e => setProject(e.target.value)} placeholder="your-project" />
      </Field></div>
      <Button type="button" variant="outline" disabled={busy || (!values.azure_devops_org && !locked("azure_devops_org"))} onClick={() => void browse()}>{busy ? "Loading…" : "Browse repos"}</Button>
    </div>
    {busy && !current && <p className="text-sm muted" role="status">Loading repositories…</p>}
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
          <span className="min-w-0 flex-1 break-words">{repo.name}</span><span className="text-xs muted">{repo.archived ? "disabled" : ""}</span>
        </label>)}
        {!visibleRepos.length && <p className="p-3 text-sm muted">{query ? "No repositories match your search." : "No repositories available for this token and organization."}</p>}
      </div>
      {current.has_more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void browse(true)}>Load more repositories</Button>}
    </div>}
    {selected.length > 0 && !current && <details className="details">
      <summary>{selected.length} {selected.length === 1 ? "repository" : "repositories"} selected</summary>
      <div className="flex flex-wrap gap-2">{selected.map(repo => <span key={repo} className="inline-flex items-center gap-2 rounded-md border px-2 py-1 text-xs">
        {repo}<button type="button" disabled={locked("repos")} aria-label={`Remove ${repo}`} onClick={() => toggle(repo, false)}>×</button>
      </span>)}</div>
    </details>}
    <details className="details">
      <summary>Repository names manually</summary>
      <div className="form-fields pt-4">
        <Field label="Repository names" locked={locked("repos")} help="One project/repo or Azure DevOps repository URL per line.">
          <Textarea name="repos" rows={3} value={values.repos} disabled={locked("repos")} onChange={e => update("repos", e.target.value)} placeholder="your-project/your-repo" />
        </Field>
      </div>
    </details>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </div>;
}
