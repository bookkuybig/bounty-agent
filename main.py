"""bounty-agent CLI.

    python main.py collect          one collection cycle (what the scheduler runs)
    python main.py report           ranked candidates
    python main.py report --json    same, for feeding an LLM triage step
    python main.py stats            what got dropped and why
    python main.py rate             GitHub API budget remaining
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from bounty_agent import collect, config, db, gh, notify, report, sources


def cmd_collect(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    started = datetime.now(timezone.utc)
    print(f"[{started.isoformat(timespec='seconds')}] collecting…")
    collector = collect.Collector(conn, cfg)
    try:
        found, kept = collector.run()
    except gh.GhError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if collector.new_hits:
        print(f"{len(collector.new_hits)} newly passing")
        notify.announce(
            collector.new_hits, config.ALERTS_PATH, cfg.get("notify", {})
        )

    taskbounty_url = cfg.get("sources", {}).get("taskbounty_url")
    if taskbounty_url:
        fresh = sources.new_taskbounty_items(taskbounty_url, config.TASKBOUNTY_SEEN_PATH)
        if fresh:
            print(f"{len(fresh)} new TaskBounty items")
            notify.announce_raw(
                "TaskBounty", fresh, config.ALERTS_PATH, cfg.get("notify", {})
            )

    mlcontests_url = cfg.get("sources", {}).get("mlcontests_url")
    if mlcontests_url:
        fresh = sources.new_mlcontests_items(
            mlcontests_url,
            config.MLCONTESTS_SEEN_PATH,
            cfg.get("sources", {}).get("mlcontests_min_prize", 0),
        )
        if fresh:
            print(f"{len(fresh)} new ML competitions")
            notify.announce_raw(
                "mlcontests", fresh, config.ALERTS_PATH, cfg.get("notify", {})
            )
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"found {found} issues, {kept} passed filters, {elapsed:.1f}s")
    if not args.quiet:
        print()
        print(report.table(db.live_issues(conn, 10, cfg["filter"].get("max_per_repo", 0))))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = config.load()
    conn = db.connect(config.DB_PATH)
    rows = db.live_issues(conn, args.limit, cfg["filter"].get("max_per_repo", 0))
    print(report.as_json(rows) if args.json else report.table(rows))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    print(report.stats(db.connect(config.DB_PATH)))
    return 0


def cmd_rate(_: argparse.Namespace) -> int:
    payload = gh.rate_limit()
    for name in ("core", "search"):
        bucket = payload["resources"][name]
        reset = datetime.fromtimestamp(bucket["reset"], timezone.utc)
        print(f"{name:<8} {bucket['remaining']}/{bucket['limit']} resets {reset:%H:%M:%S} UTC")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Scheduled runs redirect to collect.log, where Windows defaults to cp1252
    # and any emoji label in a message would crash the cycle.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="bounty-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="run one collection cycle")
    p_collect.add_argument("-q", "--quiet", action="store_true", help="suppress the table")
    p_collect.add_argument("--log", help="append all output to this file (for windowless runs)")
    p_collect.set_defaults(func=cmd_collect)

    p_report = sub.add_parser("report", help="show ranked candidates")
    p_report.add_argument("-n", "--limit", type=int, default=25)
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    sub.add_parser("stats", help="filter and cycle diagnostics").set_defaults(func=cmd_stats)
    sub.add_parser("rate", help="GitHub rate limit status").set_defaults(func=cmd_rate)

    args = parser.parse_args(argv)
    if getattr(args, "log", None):
        # pythonw has no console, so sys.stdout is None until pointed somewhere.
        log = open(args.log, "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stderr = log
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
