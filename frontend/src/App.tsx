import { useCallback, useEffect, useState } from "react";
import { BarChart3, Download, ExternalLink, GitPullRequest, RefreshCw, Settings2, Users } from "lucide-react";
import { Sidebar, SidebarHeader, SidebarContent, SidebarMenu, SidebarMenuItem, SidebarMenuButton } from "@/components/shared/Sidebar";
import { PageHeader } from "@/components/shared/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { SettingsForm } from "./SettingsForm";
import { api, money, number, date, safeURL, errorMessage, type AppState, type Pull, type Report } from "./api";

type Page = "overview" | "people" | "settings";
const navigation = [
  { id: "overview", label: "Overview", icon: BarChart3 },
  { id: "people", label: "People", icon: Users },
  { id: "settings", label: "Settings", icon: Settings2 },
] as const;
const demo = new URLSearchParams(location.search).get("demo") === "1";
const mode = demo ? "demo" : "live";

export function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [page, setPage] = useState<Page>(new URLSearchParams(location.search).get("page") === "settings" ? "settings" : "overview");
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const [selectedPR, selectPR] = useState<Pull | null>(null);
  const [matching, setMatching] = useState<{ login: string; email: string } | null>(null);
  const [matchError, setMatchError] = useState("");

  const refresh = useCallback(async () => { setState(await api<AppState>("/api/state?mode=" + mode)); }, []);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll(first = false) {
      try {
        const result = await api<AppState>("/api/state?mode=" + mode);
        if (!active) return;
        setState(result);
        setConnectionError("");
        if (first && !result.settings.ready && !result.report) setPage("settings");
      } catch (error) { if (active) setConnectionError(errorMessage(error)); }
      if (active) timer = setTimeout(() => void poll(), 3000);
    }
    void poll(true);
    return () => { active = false; clearTimeout(timer); };
  }, []);

  function navigate(next: Page) {
    if (demo && next === "settings") { location.assign("/?page=settings"); return; }
    setPage(next); setError(""); window.scrollTo(0, 0);
  }
  async function startSync() {
    if (demo) return;
    await api("/api/sync", "POST", {});
    await refresh(); setPage("overview");
  }
  async function syncAction(cancel = false) {
    setBusy(true); setError("");
    try {
      if (cancel) { await api("/api/sync", "DELETE"); await refresh(); }
      else await startSync();
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(false); }
  }
  async function saveMatch(remove = false) {
    if (!matching || !state || demo) return;
    setBusy(true); setMatchError("");
    const identity_map = { ...state.settings.identity_map };
    if (remove) delete identity_map[matching.login.toLowerCase()];
    else identity_map[matching.login.toLowerCase()] = matching.email;
    try {
      await api("/api/settings", "PUT", { identity_map });
      await refresh(); setMatching(null);
    } catch (error) { setMatchError(errorMessage(error)); }
    finally { setBusy(false); }
  }

  if (!state) return <main className="p-8 text-sm" role={connectionError ? "alert" : "status"}>
    {connectionError || "Loading…"}{connectionError && <Button className="ml-3" variant="outline" onClick={() => location.reload()}>Retry</Button>}
  </main>;

  const report = state.report;
  const activeNav = navigation.find(n => n.id === page)!;
  return <div>
    <Sidebar className="app-sidebar w-[224px] bg-muted/15">
      <SidebarHeader className="mb-3 border-b border-sidebar-border/70 px-5 py-6">
        <a href={demo ? "/?demo=1" : "/"} aria-label="LiteLLM ROI Calculator home" className="brand-link">
          <img src="/assets/litellm-icon.jpg" width={36} height={36} alt="" className="size-9 shrink-0 rounded-full" />
          <span className="flex min-w-0 flex-col gap-0.5"><span className="text-[17px] leading-5 font-semibold tracking-tight">LiteLLM</span><span className="text-xs leading-4 text-muted-foreground">ROI Calculator</span></span>
        </a>
      </SidebarHeader>
      <SidebarContent aria-label="Main navigation"><SidebarMenu className="gap-1">
        {navigation.map(item => <SidebarMenuItem key={item.id}>
          <SidebarMenuButton className="h-9" isActive={page === item.id} aria-current={page === item.id ? "page" : undefined} onClick={() => navigate(item.id)}>
            <item.icon /><span>{item.label}</span>
          </SidebarMenuButton>
        </SidebarMenuItem>)}
      </SidebarMenu></SidebarContent>
    </Sidebar>
    <main className="app-main"><div className="mx-auto max-w-7xl space-y-6">
      {demo && <div className="notice"><span>Sample data</span><Button variant="outline" size="sm" onClick={() => navigate("settings")}>Connect your data</Button></div>}
      <PageHeader title={page === "overview" ? "ROI Calculator" : page === "settings" && !state.settings.ready ? "Setup" : activeNav.label}
        icon={<activeNav.icon />}
        subtitle={page === "settings" ? "Connect a LiteLLM gateway and GitHub repositories." : page === "people" ? "Match GitHub authors to gateway users by email." : "Estimated engineering hours from merged PRs, not actual hours spent."}
        primaryAction={page !== "settings" ? <div className="flex items-center gap-3">
          <Button onClick={() => demo ? navigate("settings") : void syncAction()} disabled={busy || state.status.running || (!demo && !state.settings.ready)}>
            <RefreshCw className={state.status.running ? "animate-spin" : ""} />{state.status.running ? "Syncing…" : "Sync now"}
          </Button>
          {report && <span className="text-xs muted">{date(report.start)} – {date(report.end)} · UTC</span>}
        </div> : undefined}
        utilities={page === "settings" ? <Button variant="outline" size="sm" onClick={() => location.assign("/?demo=1")}>View sample data</Button> : report ?
          <Button variant="outline" render={<a href={`/api/export?mode=${mode}`} />} nativeButton={false}><Download />Export CSV</Button> : undefined}
      />
      {(error || connectionError || state.status.error) && <div className="notice text-destructive" role="alert">{error || connectionError || state.status.error}</div>}
      {state.status.running && <div className="notice" role="status"><span>{state.status.stage}{state.status.total > 0 && ` · ${state.status.done} / ${state.status.total}`}</span>
        <Button variant="outline" size="sm" onClick={() => void syncAction(true)} disabled={busy}>Cancel sync</Button></div>}
      {page === "settings" ? <SettingsForm state={state} refresh={refresh} onSync={startSync} /> : !report ?
        <div className="rounded-lg border p-8 text-sm muted">{state.status.running ? "Your report will appear when the sync finishes." : "No report yet. Sync to import spend and estimate merged PRs."}</div> :
        page === "overview" ? <Overview report={report} onSelect={selectPR} onPeople={() => navigate("people")} /> :
        <People report={report} onMatch={(login, email) => { setMatchError(""); setMatching({ login, email }); }} />}
      {report && page !== "settings" && <p className="text-xs muted">
        {demo ? "Sample report" : `Last synced ${new Date(report.synced_at).toLocaleString()}`}
        {!demo && ` · ${state.settings.backfill_days}-day backfill · ${state.settings.update_interval_minutes ? `Updates every ${state.settings.update_interval_minutes} minutes` : "Manual updates"}`}
      </p>}
    </div></main>
    <Dialog open={!!selectedPR} onOpenChange={open => { if (!open) selectPR(null); }}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        {selectedPR && <>
          <DialogHeader><DialogTitle className="pr-8 leading-snug">{selectedPR.title}</DialogTitle>
            <DialogDescription>{selectedPR.repo} #{selectedPR.number} · {selectedPR.login}</DialogDescription></DialogHeader>
          <div><p className="text-sm muted">Estimated engineering hours</p><p className="mt-2 text-3xl font-semibold tabular-nums">
            {selectedPR.estimate.status === "estimated" ? `${number(selectedPR.estimate.hours)} hrs` : selectedPR.estimate.status === "error" ? "Estimate failed" : "Needs review"}
          </p><p className="mt-2 text-xs muted">Model estimate, not actual hours spent or hours saved by AI.</p></div>
          <div><h3 className="mb-2 font-medium">Reasoning</h3><p className="whitespace-pre-wrap leading-relaxed">{selectedPR.estimate.reasoning || "No estimate available."}</p></div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-xs">
            <dt className="muted">Model</dt><dd className="break-all">{selectedPR.estimate.model || report?.estimator_model}</dd>
            <dt className="muted">Merged</dt><dd>{date(selectedPR.merged_at)}</dd>
            <dt className="muted">Email match</dt><dd className="break-all">{selectedPR.email || "Not matched"}</dd>
          </dl>
          {report?.estimator_prompt && <details className="details"><summary>Estimator prompt</summary><p className="whitespace-pre-wrap">{report.estimator_prompt}</p></details>}
          {selectedPR.url && <DialogFooter><Button variant="outline" render={<a href={safeURL(selectedPR.url)} target="_blank" rel="noopener noreferrer" />} nativeButton={false}>View on GitHub<ExternalLink /></Button></DialogFooter>}
        </>}
      </DialogContent>
    </Dialog>
    <Dialog open={!!matching} onOpenChange={open => { if (!open) setMatching(null); }}>
      <DialogContent>
        <DialogHeader><DialogTitle>Match email</DialogTitle><DialogDescription>Link {matching?.login} to their gateway email.</DialogDescription></DialogHeader>
        {demo ? <><p>Connect your data to edit email matches.</p><Button onClick={() => navigate("settings")}>Connect your data</Button></> :
          <form onSubmit={e => { e.preventDefault(); void saveMatch(); }} className="space-y-5">
            <label className="field"><span>Gateway email</span><Input type="email" required value={matching?.email || ""}
              onChange={e => setMatching(current => current && { ...current, email: e.target.value })} placeholder="name@company.com" list="gateway-emails" /></label>
            <datalist id="gateway-emails">{report?.people.filter(p => p.email && p.spend !== null).map(p => <option key={p.id} value={p.email} />)}</datalist>
            {matchError && <p role="alert" className="text-sm text-destructive">{matchError}</p>}
            <DialogFooter>
              {matching && state.settings.identity_map[matching.login.toLowerCase()] && <Button type="button" variant="outline" disabled={busy || state.status.running} onClick={() => void saveMatch(true)}>Use automatic match</Button>}
              <Button type="submit" disabled={busy || state.status.running}>{busy ? "Saving…" : "Save match"}</Button>
            </DialogFooter>
          </form>}
      </DialogContent>
    </Dialog>
  </div>;
}

function Overview({ report, onSelect, onPeople }: { report: Report; onSelect: (pr: Pull) => void; onPeople: () => void }) {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(25);
  const m = report.metrics;
  const filtered = report.pulls.filter(pr => `${pr.title} ${pr.repo} ${pr.number} ${pr.login}`.toLowerCase().includes(query.toLowerCase()));
  const metrics = [
    ["Estimated engineering hours", `${number(m.output_hours)} hrs`, `${m.cohort_people} matched ${m.cohort_people === 1 ? "person" : "people"} with complete estimates`],
    ["Gateway spend", money(m.matched_spend), "For the same matched people and period"],
    ["Spend per estimated hour", money(m.cost_per_hour), "Gateway spend ÷ estimated engineering hours"],
  ];
  return <>
    <div className="grid gap-4 sm:grid-cols-3">{metrics.map(([label, value, detail]) => <Card key={label} size="sm" className="rounded-lg shadow-none">
      <CardContent><h2 className="min-h-5 text-sm muted sm:min-h-8 sm:text-xs xl:min-h-5 xl:text-sm">{label}</h2><p className="my-3 text-3xl font-semibold tracking-tight tabular-nums">{value}</p><p className="text-xs muted">{detail}</p></CardContent>
    </Card>)}</div>
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs muted">
      <span>{m.matched_prs} of {m.merged_prs} PRs matched. {money(m.excluded_spend)} of {money(m.total_spend)} gateway spend excluded from the comparison.</span>
      <Button variant="link" size="xs" className="h-auto px-0 text-xs" onClick={onPeople}>Review matches</Button>
    </div>
    {report.warnings.map((warning, i) => <p key={i} role="alert" className="text-sm text-warning">{warning}</p>)}
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><h2 className="font-semibold text-base">Pull requests</h2><Badge variant="secondary">{m.merged_prs}</Badge></div>
        <Input type="search" aria-label="Search pull requests" placeholder="Search pull requests" className="w-full sm:w-64" value={query} onChange={e => { setQuery(e.target.value); setLimit(25); }} /></div>
      <div className="data-table"><Table>
        <TableHeader><TableRow><TableHead>Pull request</TableHead><TableHead>Author</TableHead><TableHead className="text-right">Estimated engineering hours</TableHead><TableHead>Merged</TableHead></TableRow></TableHeader>
        <TableBody>{filtered.slice(0, limit).map(pr => <TableRow key={`${pr.repo}#${pr.number}`}>
          <TableCell className="w-full max-w-md whitespace-normal"><button onClick={() => onSelect(pr)} className="flex w-full items-start gap-2.5 text-left hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 rounded-sm">
            <GitPullRequest className="mt-0.5 size-4 shrink-0 muted" /><span><span className="font-medium">{pr.title}</span><span className="mt-1 block text-xs muted">{pr.repo} #{pr.number}</span></span>
          </button></TableCell>
          <TableCell>{pr.login}</TableCell>
          <TableCell className="text-right tabular-nums">{pr.estimate.status === "estimated" ? `${number(pr.estimate.hours)} hrs` :
            <Button variant="link" size="xs" className="text-warning px-0" onClick={() => onSelect(pr)}>{pr.estimate.status === "error" ? "Estimate failed" : "Needs review"}</Button>}</TableCell>
          <TableCell className="muted text-xs">{date(pr.merged_at)}</TableCell>
        </TableRow>)}{filtered.length === 0 && <TableRow><TableCell colSpan={4} className="text-center muted">{query ? "No matching pull requests." : "No merged pull requests in this period."}</TableCell></TableRow>}</TableBody>
      </Table></div>
      <div className="flex justify-between items-center gap-4 text-xs muted"><span>{m.estimated_prs} estimated{m.pending_prs > 0 && ` · ${m.pending_prs} need attention`}</span>
        {filtered.length > limit && <Button variant="outline" size="sm" onClick={() => setLimit(n => n + 25)}>Show more ({filtered.length - limit})</Button>}</div>
    </section>
    <details className="details"><summary>How this is calculated</summary>
      <p>The model estimates how many engineering hours the merged PRs represent. These are not measured working hours, hours saved by AI, or financial returns.</p>
      <p className="mt-2">Spend per estimated hour includes only people with matched gateway spend and complete PR estimates, for the same period. Gateway spend includes all of each person's usage; it isn't attributed to individual PRs or repositories.</p>
    </details>
  </>;
}

function People({ report, onMatch }: { report: Report; onMatch: (login: string, email: string) => void }) {
  return <>
    <p className="text-sm muted">Engineering hours are model estimates, not actual time spent. Spend includes each person's full gateway usage for this period.</p>
    <div className="data-table"><Table>
      <TableHeader><TableRow><TableHead>GitHub user</TableHead><TableHead>Gateway email</TableHead><TableHead className="text-right">Gateway spend</TableHead><TableHead className="text-right">Estimated engineering hours</TableHead><TableHead className="text-right">Spend / estimated hour</TableHead></TableRow></TableHeader>
      <TableBody>{report.people.map(person => <TableRow key={person.id}>
        <TableCell><div className="space-y-1">{person.logins.length ? person.logins.map(login => <div key={login}><button className="font-medium hover:underline focus-visible:outline-2 rounded-sm" onClick={() => onMatch(login, person.email)}>{login}</button></div>) : "No GitHub match"}</div><p className="text-xs muted mt-1">{person.prs} {person.prs === 1 ? "PR" : "PRs"}{person.pending_prs > 0 && ` · ${person.pending_prs} pending`}</p></TableCell>
        <TableCell>{person.email || (person.logins.length ? <Button variant="link" size="sm" className="h-auto p-0" onClick={() => onMatch(person.logins[0], "")}>Match email</Button> : "Unassigned usage")}
          <p className="mt-1 text-xs muted">{person.match_methods.join(", ") || "Gateway only"}{person.logins.length > 0 && !person.eligible && " · Excluded from ratio"}</p></TableCell>
        <TableCell className="text-right tabular-nums">{money(person.spend)}</TableCell>
        <TableCell className="text-right tabular-nums">{person.estimated_prs ? `${number(person.hours)} hrs` : "—"}{person.pending_prs > 0 && <span className="text-xs muted ml-1">(partial)</span>}</TableCell>
        <TableCell className="text-right tabular-nums">{money(person.cost_per_hour)}</TableCell>
      </TableRow>)}{report.people.length === 0 && <TableRow><TableCell colSpan={5} className="text-center muted">No people in this period.</TableCell></TableRow>}</TableBody>
    </Table></div>
    <details className="details"><summary>How email matching works</summary><p>Matches use the author's public GitHub email or commit emails associated with their GitHub account. Email matching ignores case. Private, noreply, and ambiguous emails stay unmatched. Click a GitHub username to edit its match. Manual matches take priority.</p><p className="mt-2">People with no spend record or incomplete PR estimates are excluded from the ratio. Missing spend displays as “—”.</p></details>
  </>;
}
