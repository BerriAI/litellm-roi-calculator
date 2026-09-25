# Other ways to run

Return to the [setup guide](../README.md) after starting the app.

## pip

Requires Python 3.11+. From a clone of this repository:

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
litellm-roi
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell.

## Docker

```bash
docker compose up --build
```

Open http://localhost:8787. Settings and reports persist in the `roi-data` volume. The published port binds only to loopback.

## Explore without credentials

```bash
uv run litellm-roi --demo
```

This uses sample data, makes no external API calls, and does not load your saved workspace or credentials. Stop it and restart without `--demo` to connect real data. Fresh workspaces open setup; they never silently show sample reports.

On a normally started app, `?demo=1` only changes the displayed report; any live sync continues on its existing schedule.
