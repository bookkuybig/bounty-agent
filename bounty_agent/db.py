"""SQLite storage. Stdlib only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id              TEXT PRIMARY KEY,   -- owner/repo#number
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    title           TEXT,
    url             TEXT,
    created_at      TEXT,
    updated_at      TEXT,
    labels          TEXT,               -- JSON array
    assignee        TEXT,               -- login, or NULL when unassigned
    comments        INTEGER,
    body_len        INTEGER,
    amount          REAL,               -- parsed USD, NULL when unknown
    language        TEXT,
    stars           INTEGER,
    competitors     INTEGER,            -- NULL until a timeline lookup happens
    competitors_at  TEXT,               -- issue updated_at when last measured
    est_hours       REAL,
    p_win           REAL,
    score           REAL,               -- expected USD per hour
    drop_reason     TEXT,               -- NULL means it passed the filters
    first_seen      TEXT,
    last_seen       TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_score ON issues (score DESC);
CREATE INDEX IF NOT EXISTS idx_issues_live  ON issues (drop_reason, score DESC);

CREATE TABLE IF NOT EXISTS repos (
    repo        TEXT PRIMARY KEY,
    language    TEXT,
    stars       INTEGER,
    forks       INTEGER,
    is_fork     INTEGER,            -- 0/1, NULL when fetched before this column existed
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    found        INTEGER,
    kept         INTEGER,
    api_calls    INTEGER,
    error        TEXT
);
"""

UPSERT = """
INSERT INTO issues (
    id, repo, number, title, url, created_at, updated_at, labels, assignee,
    comments, body_len, amount, language, stars, competitors, competitors_at,
    est_hours, p_win, score, drop_reason, first_seen, last_seen
) VALUES (
    :id, :repo, :number, :title, :url, :created_at, :updated_at, :labels, :assignee,
    :comments, :body_len, :amount, :language, :stars, :competitors, :competitors_at,
    :est_hours, :p_win, :score, :drop_reason, :last_seen, :last_seen
)
ON CONFLICT(id) DO UPDATE SET
    title          = excluded.title,
    updated_at     = excluded.updated_at,
    labels         = excluded.labels,
    assignee       = excluded.assignee,
    comments       = excluded.comments,
    body_len       = excluded.body_len,
    amount         = excluded.amount,
    language       = excluded.language,
    stars          = excluded.stars,
    -- keep a measured competitor count unless this cycle measured a fresh one
    competitors    = COALESCE(excluded.competitors, issues.competitors),
    competitors_at = COALESCE(excluded.competitors_at, issues.competitors_at),
    est_hours      = excluded.est_hours,
    p_win          = excluded.p_win,
    score          = excluded.score,
    drop_reason    = excluded.drop_reason,
    last_seen      = excluded.last_seen
"""


def connect(path: Path) -> sqlite3.Connection:
    # A manual run can overlap the scheduled one; a cycle holds the write lock
    # for its whole duration (~20-70 s), so wait for it rather than failing.
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Columns added after the first release; CREATE TABLE IF NOT EXISTS skips them.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(repos)")}
    for name in ("forks", "is_fork"):
        if name not in columns:
            conn.execute(f"ALTER TABLE repos ADD COLUMN {name} INTEGER")
    return conn


def cached_competitors(conn: sqlite3.Connection, issue_id: str, updated_at: str) -> int | None:
    """Return a previously measured count, but only if the issue hasn't changed."""
    row = conn.execute(
        "SELECT competitors, competitors_at FROM issues WHERE id = ?", (issue_id,)
    ).fetchone()
    if row is None or row["competitors"] is None:
        return None
    return row["competitors"] if row["competitors_at"] == updated_at else None


def cached_repo(conn: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE repo = ?", (repo,)).fetchone()


def save_repo(
    conn: sqlite3.Connection,
    repo: str,
    language: str | None,
    stars: int,
    forks: int,
    is_fork: bool,
    when: str,
) -> None:
    conn.execute(
        "INSERT INTO repos (repo, language, stars, forks, is_fork, fetched_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo) DO UPDATE SET language=excluded.language,"
        " stars=excluded.stars, forks=excluded.forks, is_fork=excluded.is_fork,"
        " fetched_at=excluded.fetched_at",
        (repo, language, stars, forks, int(is_fork), when),
    )


def save_issue(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    record = dict(record)
    record["labels"] = json.dumps(record.get("labels") or [], ensure_ascii=False)
    conn.execute(UPSERT, record)


def start_run(conn: sqlite3.Connection, when: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (when,))
    conn.commit()
    return int(cur.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    when: str,
    found: int,
    kept: int,
    api_calls: int,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, found=?, kept=?, api_calls=?, error=? WHERE id=?",
        (when, found, kept, api_calls, error, run_id),
    )
    conn.commit()


def live_issues(
    conn: sqlite3.Connection, limit: int = 30, max_per_repo: int = 0
) -> list[sqlite3.Row]:
    """Top-scoring issues that passed the filters.

    `max_per_repo` keeps a repo with hundreds of bounty-labelled issues from
    filling the whole report with near-identical rows.
    """
    if max_per_repo:
        return conn.execute(
            "SELECT * FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY repo ORDER BY score DESC"
            "  ) AS rn FROM issues WHERE drop_reason IS NULL"
            ") WHERE rn <= ? ORDER BY score DESC LIMIT ?",
            (max_per_repo, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM issues WHERE drop_reason IS NULL"
        " ORDER BY score DESC NULLS LAST LIMIT ?",
        (limit,),
    ).fetchall()
