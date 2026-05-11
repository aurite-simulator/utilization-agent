"""Utilization Tracker — standalone agent reporting monthly billable utilization.

Launched by the cron worker on the 1st of each month. Reads sim_time from
Redis, analyzes the previous calendar month, and generates an LLM narrative
with workforce planning recommendations.

Classifies each consultant as: bench (0h), underutilized (<70%), normal, or
overloaded (>100%). Writes per-consultant detail to model_data/utilization/YYYY-MM.csv.
"""
import csv
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import redis

_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip().removeprefix("export").strip(), _v.strip().strip('"').strip("'"))

DB_PATH          = "model_data/firm.db"
_CAPACITY_HOURS  = 160.0  # 4 weeks × 40 h/week


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _sim_time() -> datetime:
    raw = os.environ.get("SIM_TIME")
    if not raw:
        r = redis.Redis(host="localhost", port=6379, db=1, decode_responses=True)
        raw = r.get("sim:clock:time")
    if not raw:
        raise RuntimeError("sim:clock:time not available")
    return datetime.fromisoformat(raw)


def _prev_ym(sim_time: datetime) -> str:
    first_of_current = sim_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first_of_current - timedelta(days=1)).strftime("%Y-%m")


def _consultant_hours(conn, ym: str) -> list[dict]:
    rows = conn.execute("""
        SELECT c.worker_id, bu.name AS bu, h.title,
               COALESCE(SUM(te.hours), 0) AS hours
        FROM consultants c
        JOIN business_units bu ON bu.bu_id = c.bu_id
        JOIN consultant_title_history h
          ON h.consultant_id = c.consultant_id AND h.end_date IS NULL
        LEFT JOIN time_entries te
          ON te.consultant_id = c.consultant_id AND te.year_month = ?
        GROUP BY c.consultant_id
        ORDER BY bu.name, h.title, c.worker_id
    """, (ym,)).fetchall()
    return [{"worker_id": r[0], "bu": r[1], "title": r[2], "hours": r[3]} for r in rows]


def _status(hours: float) -> str:
    rate = hours / _CAPACITY_HOURS
    if hours == 0:   return "bench"
    if rate < 0.70:  return "underutilized"
    if rate <= 1.0:  return "normal"
    return "overloaded"


# --- LLM analysis ------------------------------------------------------------

def _llm_analysis(ym: str, records: list[dict], bu_stats: dict,
                  avg_util: float, n_bench: int, n_over: int) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return

    bu_lines = []
    for bu, s in sorted(bu_stats.items()):
        bu_util = s["hours"] / (s["count"] * _CAPACITY_HOURS)
        bu_lines.append(
            f"  {bu}: {bu_util:.0%} avg utilization, {s['count']} consultants, "
            f"{s['bench']} bench, {s['overloaded']} overloaded"
        )

    overloaded = [r for r in records if r["status"] == "overloaded"]
    bench      = [r for r in records if r["status"] == "bench"]

    overloaded_list = ", ".join(
        f"{r['worker_id']} ({r['bu']}, {r['title']}, {r['hours']:.0f}h)"
        for r in overloaded[:10]
    ) or "none"
    bench_list = ", ".join(
        f"{r['worker_id']} ({r['bu']}, {r['title']})"
        for r in bench[:10]
    ) or "none"

    prompt = (
        f"You are a consulting firm workforce analyst preparing a monthly utilization briefing "
        f"for senior leadership.\n\n"
        f"Utilization data for {ym}:\n"
        f"- Firm-wide average: {avg_util:.0%} across {len(records)} consultants "
        f"({n_bench} on bench, {n_over} overloaded)\n\n"
        f"By business unit:\n" + "\n".join(bu_lines) + "\n\n"
        f"Overloaded consultants ({len(overloaded)}): {overloaded_list}\n"
        f"Bench consultants ({len(bench)}): {bench_list}\n\n"
        "Write an executive utilization report in markdown. Structure it as follows:\n\n"
        f"# Monthly Utilization Report — {ym}\n\n"
        "## Executive Summary\n"
        "2-3 sentences on overall workforce health and the single most important staffing issue.\n\n"
        "## Utilization Snapshot\n"
        "A markdown table with columns: Business Unit | Avg Utilization | Headcount | Bench | Overloaded. "
        "Include a firm-wide totals row.\n\n"
        "## Staffing Concerns\n"
        "For each BU with bench > 0 or overloaded > 0: describe the imbalance and its business impact.\n\n"
        "## Rebalancing Recommendations\n"
        "Numbered list of specific consultant moves — who should be reassigned, from where to where, "
        "and why. Be concrete.\n\n"
        "## Workforce Planning Outlook\n"
        "1-2 sentences on what to watch next month based on current trends.\n\n"
        "Use professional business language. Be direct and specific. Avoid filler phrases."
    )

    out_dir = Path("model_data") / "utilization_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        narrative = "\n".join(b.text for b in response.content if b.type == "text")
        print("[utilization] LLM analysis:")
        print(narrative)
        (out_dir / f"{ym}.md").write_text(narrative)
    except Exception as e:
        (out_dir / f"{ym}.error.log").write_text(str(e))
        print(f"[utilization] LLM error: {e}")


# --- main --------------------------------------------------------------------

def main():
    sim_time = _sim_time()
    ym       = _prev_ym(sim_time)

    conn = _connect()
    try:
        has_data = conn.execute(
            "SELECT 1 FROM time_entries WHERE year_month = ? LIMIT 1", (ym,)
        ).fetchone()
        if not has_data:
            print(f"[utilization] no data for {ym}, skipping")
            return

        records = _consultant_hours(conn, ym)
    finally:
        conn.close()

    for r in records:
        r["utilization"] = round(r["hours"] / _CAPACITY_HOURS, 3)
        r["status"]      = _status(r["hours"])

    bu_stats: dict[str, dict] = {}
    for r in records:
        s = bu_stats.setdefault(r["bu"], {"hours": 0.0, "count": 0, "bench": 0, "overloaded": 0})
        s["hours"]      += r["hours"]
        s["count"]      += 1
        s["bench"]      += r["status"] == "bench"
        s["overloaded"] += r["status"] == "overloaded"

    total_hours = sum(r["hours"] for r in records)
    n_bench     = sum(1 for r in records if r["status"] == "bench")
    n_over      = sum(1 for r in records if r["status"] == "overloaded")
    avg_util    = total_hours / (len(records) * _CAPACITY_HOURS)

    print(f"[utilization] {ym}  avg util {avg_util:.0%}  bench {n_bench}  overloaded {n_over}")
    for bu, s in sorted(bu_stats.items()):
        bu_util = s["hours"] / (s["count"] * _CAPACITY_HOURS)
        print(f"  {bu:<18} {bu_util:.0%} util  {s['bench']} bench  {s['overloaded']} overloaded")

    out_dir = Path("model_data") / "utilization"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{ym}.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["worker_id", "bu", "title", "hours", "utilization", "status"]
        )
        writer.writeheader()
        for r in records:
            writer.writerow({k: r[k] for k in writer.fieldnames})

    _llm_analysis(ym, records, bu_stats, avg_util, n_bench, n_over)


if __name__ == "__main__":
    main()
