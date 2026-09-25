# LiteLLM ROI Calculator

A local dashboard that compares LiteLLM gateway spend with **estimated engineering hours** from merged GitHub pull requests. Choose the estimator model and prompt, match people by email, and see spend per estimated engineering hour.

These are model estimates of engineering effort, not actual hours spent or hours saved by AI.

No hosted account, external database, or frontend build required. Python 3.11+.

## Run locally

```bash
git clone https://github.com/BerriAI/litellm-roi-calculator.git
cd litellm-roi-calculator
uv run litellm-roi
```

The dashboard opens at **http://localhost:8787**. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed. Alternatively:

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
litellm-roi
```

Explore the dashboard without credentials or external API calls:

```bash
uv run litellm-roi --demo
```

`--demo` runs a separate read-only mode: it does not load your saved workspace, use configured credentials, or run syncs. Stop it and restart without `--demo` to connect real data. On a normally started app, `?demo=1` only changes the displayed report; existing live syncs continue on their configured schedule.

### Docker

```bash
docker compose up --build
```

Open http://localhost:8787. Configuration and reports persist in the `roi-data` volume. The published port binds only to loopback.

## Set up in the dashboard

A fresh workspace opens with **Set up your data**, followed by a guided setup. There is no automatic sample-data fallback. Demo data is available only with `--demo` or `?demo=1`.

1. **Gateway:** your LiteLLM proxy URL and an admin or read-only admin key with access to `/user/list` and `/user/daily/activity`. Choose a dedicated inference key for estimates when using a read-only admin key.
2. **Repositories:** click **Connect GitHub**, approve read-only access on GitHub, and select repositories. The first connection includes a prefilled GitHub App registration for your copy of the calculator. Private and internal repos are supported, subject to your organization’s approval. Manual tokens and GitHub Enterprise configuration are under **Advanced**.
3. **Estimator:** select or enter a model deployment on your gateway. The model must accept `temperature: 0` and JSON-object output. Edit the default prompt if desired.
4. **Backfill and updates:** choose a rolling history window (1–3,650 days, default 30) and an update interval (default 60 minutes). Set the interval to **0 for manual only**, or at least 5 minutes for automatic updates. Both controls are available during setup and in Settings afterward.
5. Click **Start backfill**. Progress shows gateway import, repository import, and the number of PRs processed and estimated. The dashboard opens when the first report is ready. You can reload the page during backfill, cancel it, or retry after an error.

Automatic updates begin only after the first successful backfill and run while the local process is running. Saving setup does not start inference. Failed or cancelled initial backfills require an explicit retry; cached estimates are reused. On restart, the app checks the last successful report and catches up if its next update is overdue. A change to the history window takes effect on the next sync. Stop a sync with Cancel; the previous complete source snapshot is retained.

### Company GitHub connections

- **Private and internal repos:** use a company-approved fine-grained personal access token with read access to **Pull requests, Contents, and Metadata** on the repositories you select. Set the organization as the token’s resource owner. If your company uses classic tokens, private repo access requires the `repo` scope. The app only reads GitHub data.
- **Organization access:** approve the token if your organization requires it. For classic tokens in SAML SSO organizations, authorize the token for that organization. Organization policies and your account’s repository access still apply; this connection does not bypass them.
- **Repository picker:** leave Organization blank to list repositories accessible to the token, or enter an organization to browse its repositories. Use the filter and Load more for larger lists. Select up to 50 repositories per workspace. The same picker is available in Settings.
- **GitHub Enterprise Server:** expand **Advanced connection settings** and enter your API URL, such as `https://github.company.com/api/v3`. Enterprise Cloud with data residency can use `https://api.company.ghe.com`. Use a token issued for that server. Full repository URLs must match the configured server; `owner/repo` works for either.
- **Company networks:** run the app on a machine with access to the gateway and GitHub server, including your VPN if needed. For a company certificate authority, configure HTTPX’s `SSL_CERT_FILE` or `SSL_CERT_DIR`; TLS verification stays enabled.

Public repositories can be entered manually without a token, subject to GitHub’s lower anonymous rate limits. GitHub email visibility can be restricted in company accounts; use **People → Match email** when an automatic match is unavailable.

### GitHub connection

**Connect GitHub** registers a GitHub App for this deployment, then opens GitHub’s account and repository access screen. You own the App. It requests only read access to Contents, Pull requests, and Metadata; it does not subscribe to webhooks or modify code. GitHub may ask you to confirm your account before creating the App. Organization owners may need to approve installation.

After installation, GitHub asks you to authorize the connection so the calculator can verify that the installation belongs to an account you can access. The resulting user token is used only for that check and is not saved. Background updates use short-lived installation tokens that renew automatically. Each connected installation shares its approved repositories with this calculator workspace. Click **Manage GitHub access** to connect another account or change available repos, then select which repos to measure. Up to 50 repositories can be selected across connected accounts.

This is a repository connection, **not a login gate for the dashboard**. No Google OAuth or built-in SSO is required. To use an existing GitHub App instead, configure `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`, and `GITHUB_APP_PRIVATE_KEY` on the server. Set its callback URL to `<your-origin>/github/callback`, setup URL to `<your-origin>/github/installed`, disable webhooks and OAuth-on-install, and grant the read permissions above. The guided connection supports github.com; Enterprise servers use the manual connection under Advanced.

### Optional environment configuration

Copy `.env.example` to `.env` and uncomment the fields you want to manage through your environment. Nonempty environment values override dashboard settings and are labeled in the UI. Empty or whitespace-only values are ignored, leaving those fields editable in the dashboard. Never commit credentials.

| Variable | Purpose |
| --- | --- |
| `LITELLM_GATEWAY_URL` | Gateway base URL, with or without `/v1` |
| `LITELLM_ADMIN_KEY` | Read users, activity, and available models |
| `LITELLM_ESTIMATOR_KEY` | Optional separate inference key; falls back to admin key |
| `LITELLM_ESTIMATOR_MODEL` | Exact model/deployment name on the gateway |
| `GITHUB_TOKEN` | GitHub token, optional for public repositories |
| `GITHUB_REPOS` | Comma-separated repository names |
| `GITHUB_API_URL` | Optional Enterprise API URL, e.g. `https://github.example.com/api/v3` |
| `ROI_DATA_DIR` | Data location; defaults to `~/.litellm-roi` |
| `ROI_PUBLIC_URL` | HTTPS origin when hosted; Render supplies `RENDER_EXTERNAL_URL` automatically |

The default prompt is intentionally simple and contains no hour anchors or examples:

> Estimate how many hours it would take an engineer to complete the work in this pull request. Explain your estimate briefly.

The application adds a JSON response contract and tells the estimator to treat PR content as evidence, not instructions. Every request uses **temperature 0**. It sends the title, description, and file diffs—not elapsed PR duration or a suggested hour range. Models that do not support temperature 0 are not silently given a different temperature.

## What the dashboard calculates

```text
Spend per estimated hour = matched gateway spend / matched estimated engineering hours
```

- **Merged PRs only**, selected by UTC merge date. Gateway spend uses UTC daily activity for the same inclusive period; it does not use resettable user-budget spend counters.
- **Matched cohort:** people with observed gateway spend, merged PRs, and complete estimates for their imported PRs. Both sides of the ratio use that same set of people. Zero denominators show “—”.
- **All spending stays visible.** People without imported PRs, unassigned usage, and people with incomplete estimates are excluded from the ratio but remain in the total and coverage disclosure.
- **A person's spend is not a PR's cost.** The numerator includes all their gateway usage in the period, while output covers the selected repositories. The app does not invent per-PR or per-repo spend attribution. Choosing a subset of repos limits output coverage.
- **This is an output/spend comparison, not causal financial ROI.** Estimated hours describe the work represented by code changes, not hours actually worked, payroll savings, business value, or hours saved by AI. It does not claim that all scored work was AI-generated.
- **Gateway spend is gateway-reported usage cost**, not necessarily an invoice including subscriptions, discounts, or credits. Costs outside the connected gateway are not included.

### Matching people

Gateway users are matched by email to the PR author's public profile email or commit emails **associated with that same GitHub account**. Matching trims whitespace and ignores case. It does not guess from names, strip email aliases, or use another contributor's commit email. GitHub noreply addresses and ambiguous matches remain unmatched.

In **People**, click **Match email** to connect a GitHub username to a gateway email. Manual overrides take priority and immediately recalculate the report without re-estimating PRs. Multiple GitHub identities can map to the same gateway email without duplicating spend. PR effort is attributed to the PR author, not split among reviewers and co-authors.

### Estimation coverage and caching

- Inspect every estimate's reasoning, model, and status by opening a PR.
- Cached estimates are keyed by gateway URL, model, prompt, schema, head SHA, and exact PR evidence. Unchanged PRs reuse their estimate. Changing the model, prompt, title, description, or diff generates a new estimate.
- Incomplete or missing diffs and PRs above the 160,000-character input limit are marked **Needs review**. The app never silently truncates a PR and presents a full estimate.
- Failed model calls are visible and retried on a future sync. They are not cached as zero hours.
- Sync reads gateway spend **before** making estimator calls. Estimator calls can appear in subsequent gateway activity. Use a dedicated estimator key owned by a service user with no PRs to keep that spend outside the matched cohort. Requests are tagged `litellm-roi-estimator`.
- Repository or gateway import failures preserve the last report. Individual estimation failures produce a report with explicitly incomplete coverage.

## Data and connections

The app reads GitHub repositories and gateway accounting data. It writes only local configuration/reports and makes inference calls to your chosen gateway. No data is sent to a separate analytics service.

**PR titles, descriptions, and code diffs are sent to your configured estimator model through the gateway.** Your gateway/provider's handling applies. Tokens are never returned to the browser. Local `config.json` and `github-app.json` store credentials in plaintext with owner-only permissions (`0600`); protect the machine and data-directory backups. GitHub connection state uses a short-lived HttpOnly, SameSite cookie (Secure when hosted); it is not a dashboard login session. SQLite stores report metadata, email matches, model reasoning, and cached estimates. Raw diffs are not persisted by this application.

The app binds to `127.0.0.1` by default and uses no CDN assets. Locally it rejects non-local Host headers; hosted deployments accept only their configured public origin and reject cross-origin API requests. `--host 0.0.0.0` supports Docker or hosting. This is one shared workspace, not a multi-tenant service.

There is no built-in password, login, or user authorization. The localhost checks are not a substitute for authentication. If you share the app through a server or tunnel, protect the entire app and its API with an authentication layer such as your company's SSO. Anyone with access to the unprotected app can view reports, change settings, and trigger syncs using the configured credentials. Publishing this repository does not host your local dashboard or its data.

## Host on Render

[Deploy to Render](https://render.com/deploy?repo=https://github.com/BerriAI/litellm-roi-calculator) using the included `render.yaml`. It runs one Python service with a 1 GB persistent disk for settings, GitHub App credentials, and SQLite reports. No frontend build or external database is needed. Render supplies the public URL and port automatically. The smallest paid service and disk currently cost approximately **$7.25/month**, before other usage.

Open the service URL and go through setup. The deployment starts empty and makes no inference calls until you start the initial backfill. Keep one instance while using SQLite. Back up the disk; deleting it deletes the workspace.

There is intentionally **no built-in dashboard login**. Put your hosting provider’s access controls or an SSO proxy in front of the entire site and API when sharing company data. GitHub connection callbacks must return to the same HTTPS origin in `ROI_PUBLIC_URL`; set this explicitly if using a custom domain or access proxy. Anyone who can reach an unprotected deployment can use its shared workspace.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run litellm-roi --no-browser
```

FastAPI + HTTPX + SQLite, with React and LiteLLM's UI primitives. The built frontend is checked in and included in the Python package. Running the app requires no Node installation or database server. Integration tests use mocked GitHub, gateway, and inference responses and make no paid model calls.

To change the frontend (Node 22.12+):

```bash
cd frontend
npm ci
npm run build
```

The build replaces `litellm_roi/static`. Reload the dashboard after building. Commit the source and built assets together. The sidebar, page header, buttons, forms, tables, dialogs, theme, and logo come from the LiteLLM dashboard; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for provenance and license.

## License

Apache 2.0. Vendored LiteLLM UI components and logo retain their MIT notice.
