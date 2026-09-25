import { useCallback, useEffect, useState } from "react";
import { BarChart3, Download, ExternalLink, RefreshCw, Settings2, Users } from "lucide-react";
import { Sidebar, SidebarHeader, SidebarContent, SidebarMenu, SidebarMenuItem, SidebarMenuButton } from "@/components/shared/Sidebar";
import { PageHeader } from "@/components/shared/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { SettingsForm } from "./SettingsForm";
import { Onboarding } from "./Onboarding";
import { SyncProgress } from "./SyncProgress";
import { api, money, number, date, safeURL, errorMessage, type AppState, type Pull, type Report } from "./api";

type Page = "overview" | "people" | "settings";
const navigation = [
  { id: "overview", label: "Overview", icon: BarChart3 },
  { id: "people", label: "People", icon: Users },
  { id: "settings", label: "Settings", icon: Settings2 },
] as const;
const demoView = new URLSearchParams(location.search).get("demo") === "1";
const mode = demoView ? "demo" : "live";
const effortNote = (basis?: string | null) => basis === "without_ai"
  ? "Estimated engineering hours without AI assistance, not actual hours worked or hours saved."
  : "Earlier estimates did not specify AI assistance. Sync to estimate engineering hours without AI.";

export function App() {
  const [state, setState] = useState<AppState | null>(null);
  const [selectedPage, setPage] = useState<Page>(new URLSearchParams(location.search).get("page") === "settings" ? "settings" : "overview");
  const demo = demoView || !!state?.demo_only;
  const page = demo && selectedPage === "settings" ? "overview" : selectedPage;
  const [demoHelp, setDemoHelp] = useState(false);
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
    async function poll() {
      try {
        const result = await api<AppState>("/api/state?mode=" + mode);
        if (!active) return;
        setState(result);
        setConnectionError("");
      } catch (error) { if (active) setConnectionError(errorMessage(error)); }
      if (active) timer = setTimeout(() => void poll(), 3000);
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  function navigate(next: Page) {
    if (demo && next === "settings") {
      if (state?.demo_only) { setMatching(null); setDemoHelp(true); }
      else location.assign("/");
      return;
    }
    setPage(next); setError(""); window.scrollTo(0, 0);
  }
  async function startSync() {
    if (demo) return;
    await api("/api/sync", "POST", {});
    await refresh(); navigate("overview");
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

  if (!demo && !state.report) return <Onboarding state={state} refresh={refresh} connectionError={connectionError} />;

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
    <main className="app-main"><div className="page-content">
      <div className="page-heading">
        <PageHeader title={page === "settings" && !state.settings.ready ? "Setup" : activeNav.label}
          icon={<activeNav.icon />}
          subtitle={page === "settings" ? "Connect a LiteLLM gateway and GitHub repositories." : report ? `${date(report.start)} – ${date(report.end)}` : "Gateway spend and estimated engineering effort."}
        />
        <div className="page-actions">
          {page === "settings" ? null : demo ?
            <><Badge variant="secondary">Sample data</Badge><Button variant="outline" onClick={() => navigate("settings")}>Set up your data</Button></> :
            <Button variant="outline" onClick={() => void syncAction()} disabled={busy || state.status.running || !state.settings.ready}>
              <RefreshCw className={state.status.running ? "animate-spin" : ""} />{state.status.running ? "Syncing…" : "Sync now"}
            </Button>}
          {page === "people" && report && <Button variant="ghost" render={<a href={`/api/export?mode=${mode}`} />} nativeButton={false}><Download />Export CSV</Button>}
        </div>
      </div>
      {(error || connectionError || state.status.error) && <div className="notice text-destructive" role="alert">{error || connectionError || state.status.error}</div>}
      {state.status.running && <div className="notice"><div className="min-w-0 flex-1"><SyncProgress status={state.status} /></div>
        <Button variant="outline" size="sm" onClick={() => void syncAction(true)} disabled={busy}>Cancel sync</Button></div>}
      {page === "settings" ? <SettingsForm state={state} refresh={refresh} onSync={startSync} /> : !report ?
        <div className="rounded-lg border p-8 text-sm muted">{state.status.running ? "Your report will appear when the sync finishes." : "No report yet. Sync to import spend and estimate merged PRs."}</div> :
        page === "overview" ? <Overview report={report} onSelect={selectPR} onPeople={() => navigate("people")} /> :
        <People report={report} onMatch={(login, email) => { setMatchError(""); setMatching({ login, email }); }} />}
      {report && page !== "settings" && !demo && <p className="text-sm muted">
        Last synced {new Date(report.synced_at).toLocaleString()}
        {` · ${state.settings.update_interval_minutes ? `Updates every ${state.settings.update_interval_minutes} minutes` : "Manual updates"}`}
      </p>}
    </div></main>
    <Dialog open={demoHelp} onOpenChange={setDemoHelp}>
      <DialogContent>
        <DialogHeader><DialogTitle>Connect your data</DialogTitle>
          <DialogDescription>Stop the demo and restart without <code>--demo</code> to connect your gateway and repositories.</DialogDescription></DialogHeader>
        <code className="rounded-md bg-muted p-3 text-sm">uv run litellm-roi</code>
        <DialogFooter><Button onClick={() => setDemoHelp(false)}>Got it</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={!!selectedPR} onOpenChange={open => { if (!open) selectPR(null); }}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl">
        {selectedPR && <>
          <DialogHeader><DialogTitle className="pr-8 leading-snug">{selectedPR.title}</DialogTitle>
            <DialogDescription>{selectedPR.repo} #{selectedPR.number} · {selectedPR.login}</DialogDescription></DialogHeader>
          <div><p className="text-sm muted">Estimated engineering hours{selectedPR.estimate.effort_basis === "without_ai" && " without AI"}</p><p className="mt-2 text-3xl font-semibold tabular-nums">
            {selectedPR.estimate.status === "estimated" ? `${number(selectedPR.estimate.hours)} hrs` : selectedPR.estimate.status === "error" ? "Estimate failed" : "Needs review"}
          </p><p className="mt-2 text-xs muted">{effortNote(selectedPR.estimate.effort_basis || report?.effort_basis)}</p>
          {selectedPR.estimate.evidence_source === "pr_metadata" && <p className="mt-1 text-xs muted">Based on PR descriptions, file change counts, and commit metadata.</p>}</div>
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
        {demo ? <><p>Set up your data to edit email matches.</p><Button onClick={() => navigate("settings")}>Set up your data</Button></> :
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
  const [limit, setLimit] = useState(5);
  const m = report.metrics;
  const filtered = report.pulls.filter(pr => `${pr.title} ${pr.repo} ${pr.number} ${pr.login}`.toLowerCase().includes(query.toLowerCase()));
  return <>
    <section aria-label="Spend and estimated engineering effort" className="summary-section">
      <Card className="summary-card gap-0 rounded-lg py-0 shadow-none">
        <CardContent className="summary-content px-8">
          <div className="summary-result">
            <h2 className="text-base font-medium">Spend per estimated engineering hour</h2>
            <p className="summary-value">{money(m.cost_per_hour)}</p>
            <p className="text-sm muted">{m.cost_per_hour == null ? "Not enough matched data to calculate a rate." : `${m.cohort_people} matched ${m.cohort_people === 1 ? "person" : "people"} with complete estimates`}</p>
          </div>
          <dl className="summary-inputs">
            <div><dt>Gateway spend</dt><dd>{money(m.matched_spend)}</dd></div>
            <div><dt>Estimated engineering hours{report.effort_basis === "without_ai" && " without AI"}</dt><dd>{number(m.output_hours)} <span className="text-base font-normal muted">hrs</span></dd></div>
          </dl>
        </CardContent>
        <p className="summary-note">{effortNote(report.effort_basis)}</p>
      </Card>
      <details className="details calculation-details"><summary>Calculation details</summary>
        <div className="space-y-3 pt-1">
          <p>{m.cost_per_hour != null ? `${money(m.matched_spend)} gateway spend ÷ ${number(m.output_hours)} estimated engineering hours = ${money(m.cost_per_hour)} per estimated hour.` : "A rate is available when matched estimated hours are greater than zero."}</p>
          <p>The comparison includes {m.cohort_people} matched {m.cohort_people === 1 ? "person" : "people"} with complete PR estimates, for the same period in UTC. {m.matched_prs} of {m.merged_prs} PRs have email matches. {money(m.excluded_spend)} of {money(m.total_spend)} total gateway spend is excluded.</p>
          <p>Gateway spend includes all of each person's usage, across repositories. This does not measure hours saved by AI or financial returns.</p>
          <Button variant="link" className="h-auto p-0" onClick={onPeople}>Review email matches</Button>
        </div>
      </details>
    </section>
    {report.warnings.map((warning, i) => <p key={i} role="alert" className="text-sm text-warning">{warning}</p>)}
    <section className="pull-section">
      <div className="flex flex-wrap items-center justify-between gap-4"><div><h2 className="font-semibold text-lg">Pull requests</h2><p className="mt-1 text-sm muted">{m.merged_prs} merged · {m.estimated_prs} estimated{m.pending_prs > 0 && ` · ${m.pending_prs} need attention`}</p></div>
        <Input type="search" aria-label="Search pull requests" placeholder="Search pull requests" className="w-full sm:w-56" value={query} onChange={e => { setQuery(e.target.value); setLimit(e.target.value ? 25 : 5); }} /></div>
      <div className="data-table pull-table"><Table>
        <TableHeader><TableRow><TableHead>Pull request</TableHead><TableHead className="text-right">Estimated hours</TableHead></TableRow></TableHeader>
        <TableBody>{filtered.slice(0, limit).map(pr => <TableRow key={`${pr.repo}#${pr.number}`}>
          <TableCell className="whitespace-normal"><button onClick={() => onSelect(pr)} className="pr-link text-left hover:underline focus-visible:outline-2 focus-visible:outline-offset-4 rounded-sm">
            <span className="pr-title">{pr.title}</span><span className="pr-meta">{pr.repo} #{pr.number} · {pr.login}</span>
          </button></TableCell>
          <TableCell className="text-right text-base tabular-nums">{pr.estimate.status === "estimated" ? <>{number(pr.estimate.hours)} <span className="text-sm muted">hrs</span></> :
            <Button variant="link" size="xs" className="text-warning px-0" onClick={() => onSelect(pr)}>{pr.estimate.status === "error" ? "Estimate failed" : "Needs review"}</Button>}</TableCell>
        </TableRow>)}{filtered.length === 0 && <TableRow><TableCell colSpan={2} className="text-center muted">{query ? "No matching pull requests." : "No merged pull requests in this period."}</TableCell></TableRow>}</TableBody>
      </Table></div>
      <div className="flex justify-between items-center gap-4 text-sm muted"><span>Showing {Math.min(limit, filtered.length)} of {filtered.length}</span>
        {filtered.length > limit ? <Button variant="outline" onClick={() => setLimit(n => n + 25)}>{filtered.length <= limit + 25 ? `View all ${filtered.length} pull requests` : "View more pull requests"}</Button> : limit > 5 && !query && <Button variant="ghost" onClick={() => setLimit(5)}>Show fewer</Button>}</div>
    </section>
  </>;
}

function People({ report, onMatch }: { report: Report; onMatch: (login: string, email: string) => void }) {
  return <>
    <p className="text-sm leading-relaxed muted">{effortNote(report.effort_basis)} Spend includes each person's full gateway usage for this period.</p>
    <div className="data-table"><Table>
      <TableHeader><TableRow><TableHead>Person</TableHead><TableHead className="text-right">Gateway spend</TableHead><TableHead className="text-right">Estimated hours</TableHead><TableHead className="text-right">Spend / est. hour</TableHead></TableRow></TableHeader>
      <TableBody>{report.people.map(person => <TableRow key={person.id}>
        <TableCell><div className="space-y-1">{person.logins.length ? person.logins.map(login => <div key={login}><button className="font-medium hover:underline focus-visible:outline-2 rounded-sm" onClick={() => onMatch(login, person.email)}>{login}</button></div>) : "No GitHub match"}</div>
          <div className="mt-1 text-[13px] muted">{person.email || (person.logins.length ? <Button variant="link" size="sm" className="h-auto p-0" onClick={() => onMatch(person.logins[0], "")}>Match email</Button> : "Unassigned usage")}</div>
          {person.logins.length > 0 && !person.eligible && <p className="mt-1 text-[13px] muted">Excluded from ratio</p>}</TableCell>
        <TableCell className="text-right tabular-nums">{money(person.spend)}</TableCell>
        <TableCell className="text-right tabular-nums">{person.estimated_prs ? `${number(person.hours)} hrs` : "—"}<p className="mt-1 text-[13px] muted">{person.prs} {person.prs === 1 ? "PR" : "PRs"}{person.pending_prs > 0 && ` · ${person.pending_prs} pending`}</p></TableCell>
        <TableCell className="text-right tabular-nums">{money(person.cost_per_hour)}</TableCell>
      </TableRow>)}{report.people.length === 0 && <TableRow><TableCell colSpan={4} className="text-center muted">No people in this period.</TableCell></TableRow>}</TableBody>
    </Table></div>
    <details className="details"><summary>How email matching works</summary><p>Matches use the author's public GitHub email or commit emails associated with their GitHub account. Email matching ignores case. Private, noreply, and ambiguous emails stay unmatched. Click a GitHub username to edit its match. Manual matches take priority.</p><p className="mt-2">People with no spend record or incomplete PR estimates are excluded from the ratio. Missing spend displays as “—”.</p></details>
  </>;
}
