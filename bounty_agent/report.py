"""Human-readable and machine-readable output."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _age(timestamp: str | None) -> str:
    if not timestamp:
        return "?"
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    days = int((datetime.now(timezone.utc) - moment).total_seconds() // 86400)
    return f"{days}d" if days < 365 else f"{days // 365}y"


def _clip(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No candidates passed the filters. Loosen config.toml [filter] or wait for a cycle."

    header = f"{'$/hr':>7}  {'bounty':>7}  {'rivals':>6}  {'p':>5}  {'hrs':>4}  {'age':>5}  {'repo':<28}  title"
    lines = [header, "-" * len(header)]
    for row in rows:
        amount = f"${row['amount']:.0f}" if row["amount"] is not None else "?"
        rivals = "?" if row["competitors"] is None else str(row["competitors"])
        lines.append(
            f"{row['score'] or 0:>7.1f}  {amount:>7}  {rivals:>6}  "
            f"{(row['p_win'] or 0):>5.2f}  {row['est_hours'] or 0:>4.1f}  "
            f"{_age(row['created_at']):>5}  {_clip(row['repo'], 28):<28}  {_clip(row['title'], 60)}"
        )
        lines.append(f"{'':>7}  {row['url']}")
    return "\n".join(lines)


def as_json(rows: list[sqlite3.Row]) -> str:
    payload = []
    for row in rows:
        item = dict(row)
        item["labels"] = json.loads(item.get("labels") or "[]")
        payload.append(item)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def stats(conn: sqlite3.Connection) -> str:
    total = conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM issues WHERE drop_reason IS NULL").fetchone()[0]
    measured = conn.execute("SELECT COUNT(*) FROM issues WHERE competitors IS NOT NULL").fetchone()[0]
    lines = [
        f"issues tracked : {total}",
        f"passing filters: {live}",
        f"rivals measured: {measured}",
        "",
        "drop reasons:",
    ]
    reasons = conn.execute(
        "SELECT drop_reason, COUNT(*) n FROM issues WHERE drop_reason IS NOT NULL"
        " GROUP BY drop_reason ORDER BY n DESC LIMIT 12"
    ).fetchall()
    lines += [f"  {r['drop_reason']:<24} {r['n']}" for r in reasons] or ["  (none)"]

    lines += ["", "recent cycles:"]
    runs = conn.execute(
        "SELECT started_at, found, kept, api_calls, error FROM runs"
        " ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for run in runs:
        flag = f"  ERROR {run['error']}" if run["error"] else ""
        lines.append(
            f"  {run['started_at']}  found={run['found']} kept={run['kept']}"
            f" api={run['api_calls']}{flag}"
        )
    return "\n".join(lines)
