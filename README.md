# Utilization Agent

A monthly agent that analyzes consultant billable utilization and generates an executive workforce briefing using Claude AI.

## What It Does

Fires on the 1st of each month via the simulation's cron worker. For each run it:

1. Queries the firm database for the previous calendar month's time entries
2. Classifies each consultant as:
   - **bench** — 0 hours logged
   - **underutilized** — below 70% of 160h monthly capacity
   - **normal** — 70–100% of capacity
   - **overloaded** — above 100% of capacity
3. Computes per-BU rollups (avg utilization, bench count, overloaded count)
4. Calls `claude-opus-4-7` to generate a narrative workforce planning report in markdown
5. Writes per-consultant detail to `model_data/utilization/YYYY-MM.csv`
6. Writes the markdown report to `model_data/utilization_analysis/YYYY-MM.md`

## Installation

Clone into the framework's `agents/` directory and run the installer:

```bash
git clone https://github.com/aurite-simulator/utilization-agent agents/utilization
bash agents/utilization/install.sh
```

`install.sh` installs dependencies into the shared virtualenv and appends the monthly cron entry to the model's `crontab` file (idempotent — safe to run multiple times).

## Configuration

Create a `.env` file in this directory with your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

If no API key is present the agent still runs and writes the CSV — the LLM narrative is silently skipped.

## Running

The agent is launched automatically by the simulation's cron worker on the 1st of each month. To run manually:

```bash
source venv/bin/activate
python agents/utilization/utilization.py
```

The simulation must be running (or have recently run) so that `sim:clock:time` is available in Redis.

## Output

| File | Description |
|------|-------------|
| `model_data/utilization/YYYY-MM.csv` | Per-consultant hours, utilization rate, and status |
| `model_data/utilization_analysis/YYYY-MM.md` | Executive workforce report for that month |
| `model_data/utilization_analysis/YYYY-MM.error.log` | LLM error details if the API call fails |
