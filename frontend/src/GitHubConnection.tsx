import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { api, errorMessage, type Settings } from "./api";
import { Field, type FormValues } from "./configuration";

type Repository = { name: string; visibility: string; archived: boolean };
type RepositoryPage = { repos: Repository[]; has_more: boolean };
export const githubConnectionFields = ["github_token", "github_api_url"];

export function GitHubConnection({ values, saved, locked, update, saveConnection, onBusy }: {
  values: FormValues; saved: Settings; locked: (name: keyof FormValues) => boolean;
  update: (name: keyof FormValues, value: string) => void;
  saveConnection: () => Promise<Settings>; onBusy: (busy: boolean) => void;
}) {
  const [organization, setOrganization] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RepositoryPage | null>(null);
  const [loadedFrom, setLoadedFrom] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const source = JSON.stringify([organization.trim(), values.github_api_url, values.github_token]);
  const current = source === loadedFrom ? result : null;
  const selected = values.repos.split(/[\n,]/).map(repo => repo.trim()).filter(Boolean);
  const normalized = (repo: string) => repo.replace(/^https?:\/\/[^/]+\//i, "").replace(/\/$/, "").replace(/\.git$/, "").toLowerCase();
  const selectedNames = selected.map(normalized);

  async function browse(more = false) {
    setBusy(true); onBusy(true); setError("");
    try {
      const connection = await saveConnection();
      const nextPage = more ? page + 1 : 1;
      const data = await api<RepositoryPage>(`/api/github/repos?org=${encodeURIComponent(organization.trim())}&page=${nextPage}`);
      setResult({ repos: more ? [...new Map([...(current?.repos || []), ...data.repos].map(repo => [repo.name, repo])).values()] : data.repos, has_more: data.has_more });
      // Saving clears the token draft. The resulting list belongs to that saved connection.
      setLoadedFrom(JSON.stringify([organization.trim(), connection.github_api_url, ""]));
      setPage(nextPage);
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); onBusy(false); }
  }

  function toggle(repo: string, checked: boolean) {
    if (checked && selected.length >= 50) { setError("Select up to 50 repositories."); return; }
    update("repos", (checked ? [...selected, repo] : selected.filter(value => normalized(value) !== repo.toLowerCase())).join("\n"));
    setError("");
  }

  return <div className="form-fields">
    <Field label="GitHub token" locked={locked("github_token")} help="For private and internal repos, use a token with read access to Pull requests, Contents, and Metadata. Your organization may need to approve it or authorize SSO.">
      <Input name="github_token" type="password" autoComplete="new-password" value={values.github_token} disabled={locked("github_token")}
        placeholder={saved.has_github_token ? "Saved. Leave blank to keep." : "github_pat_…"} onChange={e => update("github_token", e.target.value)} />
    </Field>
    <details className="details" open={values.github_api_url !== "https://api.github.com" || undefined}>
      <summary>GitHub Enterprise</summary>
      <div className="pt-2"><Field label="GitHub API URL" locked={locked("github_api_url")} help="For Enterprise Server, use https://github.company.com/api/v3. For github.com, keep the default.">
        <Input name="github_api_url" type="url" value={values.github_api_url} disabled={locked("github_api_url")}
          onChange={e => update("github_api_url", e.target.value)} required />
      </Field></div>
    </details>
    <div className="flex items-end gap-3">
      <div className="min-w-0 flex-1"><Field label="Organization (optional)">
        <Input value={organization} onChange={e => setOrganization(e.target.value)} placeholder="your-company" />
      </Field></div>
      <Button type="button" variant="outline" disabled={busy} onClick={() => void browse()}>{busy ? "Loading…" : "Browse repos"}</Button>
    </div>
    {current && <div className="repo-picker">
      <Input aria-label="Filter loaded repositories" placeholder="Filter loaded repositories…" value={query} onChange={e => setQuery(e.target.value)} />
      <div className="repo-list" aria-label="Available repositories">
        {current.repos.filter(repo => repo.name.toLowerCase().includes(query.toLowerCase())).map(repo => <label key={repo.name} className="repo-choice">
          <input type="checkbox" checked={selectedNames.includes(repo.name.toLowerCase())} disabled={locked("repos")}
            onChange={e => toggle(repo.name, e.target.checked)} />
          <span className="min-w-0 flex-1 break-words">{repo.name}</span><span className="text-xs muted">{repo.visibility}{repo.archived ? " · archived" : ""}</span>
        </label>)}
        {!current.repos.some(repo => repo.name.toLowerCase().includes(query.toLowerCase())) && <p className="p-3 text-sm muted">No repositories found. Check the organization and your token’s repository access, or enter a repository below.</p>}
      </div>
      {current.has_more && <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void browse(true)}>Load more repositories</Button>}
    </div>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <Field label="Selected repositories" locked={locked("repos")} help="Choose above or enter one owner/repo or repository URL per line. Up to 50 repos.">
      <Textarea name="repos" rows={3} value={values.repos} disabled={locked("repos")} onChange={e => update("repos", e.target.value)} placeholder="your-company/your-repo" />
    </Field>
  </div>;
}
