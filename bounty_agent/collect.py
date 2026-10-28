"""Stage 1: collect candidates, measure competition, store scored rows."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import db, gh, score

REPO_CACHE_DAYS = 7
_ASSIGNED_IN_BODY = re.compile(r"assigned to \[?@([A-Za-z0-9-]+)", re.IGNORECASE)
# Expensify's C+ reviewers sign off a chosen proposal with one of these phrases; MelvinBot's
# "Draft PR created: [...]" means the job is already being implemented.
_ACCEPTED = re.compile(r"C\+ reviewed|going with @\S+'s proposal|Draft PR created: \[", re.IGNORECASE)
_PROPOSAL = re.compile(r"^#+\s*Proposal\b", re.MULTILINE | re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_from_url(repository_url: str) -> str:
    # https://api.github.com/repos/owner/name -> owner/name
    return "/".join(repository_url.rstrip("/").split("/")[-2:])


def build_queries(cfg: dict[str, Any]) -> list[str]:
    """One query per label or commenter. Simpler and more predictable than OR-ing them."""
    queries = []
    for label in cfg["search"]["labels"]:
        queries.append(f'is:issue is:open label:"{label}" sort:created-desc')
    for login in cfg["search"].get("commenters", []):
        queries.append(f"is:issue is:open commenter:{login} sort:created-desc")
    for raw in cfg["search"].get("queries", []):
        queries.append(f"{raw} sort:created-desc")
    return queries


def normalize(item: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a search result into a storable record, or None if not an issue."""
    if "pull_request" in item:
        return None  # search/issues returns PRs too

    repo = _repo_from_url(item.get("repository_url", ""))
    if not repo or "/" not in repo:
        return None

    labels = [lbl.get("name", "") for lbl in item.get("labels", [])]
    body = item.get("body") or ""
    assignee = (item.get("assignee") or {}).get("login")
    if assignee is None:
        # Some programs (tenstorrent) hand the job out in the body text without using
        # GitHub's assignee field: "...reviewed and assigned to [@user](...)".
        claimed = _ASSIGNED_IN_BODY.search(body)
        if claimed:
            assignee = claimed.group(1)

    return {
        "id": f"{repo}#{item['number']}",
        "repo": repo,
        "number": item["number"],
        "title": item.get("title") or "",
        "url": item.get("html_url") or "",
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "labels": labels,
        "assignee": assignee,
        "comments": item.get("comments") or 0,
        "body_len": len(body),
        "amount": score.parse_amount(item.get("title") or "", labels, body),
        "language": None,
        "stars": 0,
        "forks": None,
        "is_fork": None,
        "competitors": None,
        "competitors_at": None,
        "drop_reason": None,
        "last_seen": _now(),
    }


class Collector:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.cfg = cfg
        self.api_calls = 0
        self.search_failed = False
        self.new_hits: list[dict[str, Any]] = []

    # ---- repo metadata -------------------------------------------------

    def repo_meta(self, repo: str) -> tuple[str | None, int, int | None, bool | None]:
        """Return (language, stars, forks, is_fork). Fork fields are None when unknown."""
        cached = db.cached_repo(self.conn, repo)

        def from_cache() -> tuple[str | None, int, int | None, bool | None]:
            is_fork = None if cached["is_fork"] is None else bool(cached["is_fork"])
            return cached["language"], cached["stars"] or 0, cached["forks"], is_fork

        # Rows cached before fork data existed are refetched once.
        if cached is not None and cached["is_fork"] is not None:
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now(timezone.utc) - fetched < timedelta(days=REPO_CACHE_DAYS):
                return from_cache()

        try:
            payload = gh.api(f"repos/{repo}")
            self.api_calls += 1
        except gh.GhError:
            return from_cache() if cached else (None, 0, None, None)

        language = payload.get("language")
        stars = int(payload.get("stargazers_count") or 0)
        forks = int(payload.get("forks_count") or 0)
        is_fork = bool(payload.get("fork"))
        db.save_repo(self.conn, repo, language, stars, forks, is_fork, _now())
        self.conn.commit()  # don't hold the write lock across later network calls
        return language, stars, forks, is_fork

    # ---- competition ---------------------------------------------------

    def count_proposers(self, repo: str, number: int) -> tuple[int, bool] | None:
        """Return (distinct "## Proposal" authors, a proposal already accepted).

        None when the comments could not be read: an unreadable thread must not
        look like an empty one, or every network hiccup becomes a false alert.
        """
        authors: set[str] = set()
        accepted = False
        for page in (1, 2, 3):
            try:
                comments = gh.api(
                    f"repos/{repo}/issues/{number}/comments", {"per_page": 100, "page": page}
                )
                self.api_calls += 1
            except gh.GhError:
                return None
            if not isinstance(comments, list):
                return None
            for comment in comments:
                body = comment.get("body") or ""
                if _PROPOSAL.search(body):
                    authors.add((comment.get("user") or {}).get("login") or "")
                if _ACCEPTED.search(body):
                    accepted = True
            if len(comments) < 100:
                break
        return len(authors), accepted

    def count_linked_prs(self, repo: str, number: int) -> tuple[int, int]:
        """Return (open PRs, merged PRs) that reference this issue.

        Open PRs are the competition. A merged one means the work is already
        done and the bounty is gone no matter what the issue state says.
        """
        open_prs: set[str] = set()
        merged_prs: set[str] = set()
        page = 1
        while page <= 3:
            try:
                events = gh.api(
                    f"repos/{repo}/issues/{number}/timeline",
                    {"per_page": 100, "page": page},
                )
                self.api_calls += 1
            except gh.GhError:
                break
            if not isinstance(events, list) or not events:
                break

            for event in events:
                if event.get("event") != "cross-referenced":
                    continue
                source_issue = (event.get("source") or {}).get("issue") or {}
                pr = source_issue.get("pull_request")
                if not pr:
                    continue
                url = source_issue.get("html_url") or ""
                if pr.get("merged_at"):
                    merged_prs.add(url)
                elif source_issue.get("state") == "open":
                    open_prs.add(url)

            if len(events) < 100:
                break
            page += 1

        return len(open_prs), len(merged_prs)

    def bot_reward(self, repo: str, number: int) -> float | None:
        """Read the bounty bots' own comments for a USD amount.

        Bots also comment on issues that carry no reward (Opire answers every
        `/try` with "this issue does not have any reward yet"), so a bot comment
        alone proves nothing.
        """
        bots = {login.lower() for login in getattr(self, "commenters", [])}
        try:
            comments = gh.api(f"repos/{repo}/issues/{number}/comments", {"per_page": 100})
            self.api_calls += 1
        except gh.GhError:
            return None
        amounts = [
            amount
            for comment in comments if isinstance(comments, list)
            if ((comment.get("user") or {}).get("login") or "").lower() in bots
            if (amount := score.parse_amount("", [], comment.get("body") or "")) is not None
        ]
        return max(amounts) if amounts else None

    # ---- main cycle ----------------------------------------------------

    def gather(self) -> list[dict[str, Any]]:
        limit = self.cfg["search"]["per_label_limit"]
        blocked = {name.lower() for name in self.cfg["filter"].get("blocked_repos", [])}
        seen: dict[str, dict[str, Any]] = {}
        commenters = self.cfg["search"].get("commenters", [])
        for query in build_queries(self.cfg):
            via_bot = "commenter:" in query
            try:
                items = gh.search_issues(query, limit)
                self.api_calls += 1
            except gh.GhError as exc:
                print(f"  search failed [{query}]: {exc}")
                self.search_failed = True
                continue
            for item in items:
                record = normalize(item)
                if record is None or record["repo"].lower() in blocked:
                    continue
                kept_record = seen.setdefault(record["id"], record)
                # Found by a label search at least once means the repo itself
                # marked it as a bounty; only bot-comment-only hits need proof.
                if via_bot:
                    kept_record.setdefault("_bot_only", True)
                else:
                    kept_record["_bot_only"] = False
        self.commenters = commenters
        return list(seen.values())

    def enrich(self, records: Iterable[dict[str, Any]]) -> None:
        """Attach repo metadata, then spend the timeline budget on the best candidates."""
        records = list(records)
        for record in records:
            (
                record["language"],
                record["stars"],
                record["forks"],
                record["is_fork"],
            ) = self.repo_meta(record["repo"])

        # Provisional score so the ordering below is not arbitrary.
        for record in records:
            score.score_issue(record, self.cfg)

        budget = self.cfg["api"]["timeline_budget"]
        max_per_repo = self.cfg["filter"].get("max_per_repo", 0)
        proposal_repos = {r.lower() for r in self.cfg["filter"].get("proposal_repos", [])}
        spent_on: dict[str, int] = {}

        # Anything a hard filter already rejects (farm, too old, too cheap) is not
        # worth a timeline request.
        candidates = [
            r for r in records
            if (not r.get("assignee") or score.is_exempt(r, self.cfg))
            and score.drop_reason(r, self.cfg) is None
        ]
        candidates.sort(key=score.prefilter_rank, reverse=True)

        for record in candidates:
            if record.get("_bot_only") and record.get("amount") is None:
                if budget <= 0:
                    continue
                budget -= 1
                record["amount"] = self.bot_reward(record["repo"], record["number"])
                if record["amount"] is None:
                    continue
                score.score_issue(record, self.cfg)
                if score.drop_reason(record, self.cfg) is not None:
                    continue
            by_proposal = record["repo"].lower() in proposal_repos
            if by_proposal:
                # Proposals only accumulate and an acceptance is final, so a job
                # already found crowded or taken never needs counting again.
                previous = self.conn.execute(
                    "SELECT competitors, drop_reason FROM issues WHERE id = ?", (record["id"],)
                ).fetchone()
                settled = previous is not None and (
                    (previous["drop_reason"] or "").startswith("crowded:")
                    or previous["drop_reason"] == "proposal-accepted"
                )
                if settled:
                    record["competitors"] = previous["competitors"]
                    record["competitors_at"] = record["updated_at"]
                    if previous["drop_reason"] == "proposal-accepted":
                        record["_accepted"] = True
                    continue
            # Proposal threads carry state the cache doesn't (accepted or not),
            # so open ones are always recounted.
            cached = (
                None
                if by_proposal
                else db.cached_competitors(self.conn, record["id"], record["updated_at"] or "")
            )
            if cached is not None:
                record["competitors"] = cached
                record["competitors_at"] = record["updated_at"]
                # The merged-PR finding isn't stored as a number; keep it from last time.
                previous = self.conn.execute(
                    "SELECT drop_reason FROM issues WHERE id = ?", (record["id"],)
                ).fetchone()
                if previous is not None and previous["drop_reason"] == "likely-solved":
                    record["_merged"] = 1
                continue
            if budget <= 0:
                continue
            # Spread the budget: one busy repo must not consume every lookup.
            if (
                not by_proposal
                and max_per_repo
                and spent_on.get(record["repo"], 0) >= max_per_repo
            ):
                continue
            spent_on[record["repo"]] = spent_on.get(record["repo"], 0) + 1
            budget -= 1
            # On proposal repos the PR comes after a hire, which "C+ reviewed"
            # already catches; skipping the multi-page timeline keeps cycles short.
            open_prs, merged_prs = (
                (0, 0) if by_proposal else self.count_linked_prs(record["repo"], record["number"])
            )
            if by_proposal:
                proposals = self.count_proposers(record["repo"], record["number"])
                if proposals is None:
                    record["_unmeasured"] = True
                    continue
                proposers, accepted = proposals
                open_prs += proposers
                if accepted:
                    record["_accepted"] = True
            record["competitors"] = open_prs
            record["competitors_at"] = record["updated_at"]
            if merged_prs:
                record["_merged"] = merged_prs

    def run(self) -> tuple[int, int]:
        started = _now()
        run_id = db.start_run(self.conn, started)
        found = kept = 0
        error: str | None = None
        proposal_repos = {r.lower() for r in self.cfg["filter"].get("proposal_repos", [])}
        try:
            # Blocklist additions should also clear rows collected before the ban.
            for repo in self.cfg["filter"].get("blocked_repos", []):
                self.conn.execute("DELETE FROM issues WHERE repo = ? COLLATE NOCASE", (repo,))
            # Release the write lock now; otherwise it is held through every
            # network call below and blocks manual runs for minutes.
            self.conn.commit()

            records = self.gather()
            found = len(records)
            self.enrich(records)

            for record in records:
                score.score_issue(record, self.cfg)
                reason = score.drop_reason(record, self.cfg)
                if reason is None and record.pop("_merged", 0):
                    reason = "likely-solved"
                if reason is None and record.get("_accepted"):
                    reason = "proposal-accepted"
                if (
                    reason is None
                    and record.get("competitors") is None
                    and record["repo"].lower() in proposal_repos
                ):
                    # The timeline budget ran out before this job's proposals were counted.
                    # An uncounted proposal thread is not evidence of an open job.
                    reason = "unmeasured"
                if reason is None and record.get("_unmeasured"):
                    reason = "unmeasured"  # retried next cycle; never alert on a guess
                if reason is None and record.get("_bot_only") and record.get("amount") is None:
                    # Unverified (budget ran out) or verified empty: either way no proof of money.
                    reason = "no-reward"
                record["drop_reason"] = reason
                if reason is None:
                    kept += 1
                    previous = self.conn.execute(
                        "SELECT drop_reason FROM issues WHERE id = ?", (record["id"],)
                    ).fetchone()
                    if previous is None or previous["drop_reason"] is not None:
                        self.new_hits.append(record)
                db.save_issue(self.conn, record)
            # Closed issues fall out of the `is:open` searches and would otherwise
            # stay in the report forever. Only trust absence when every search ran.
            if not self.search_failed:
                self.conn.execute(
                    "UPDATE issues SET drop_reason = 'gone'"
                    " WHERE drop_reason IS NULL AND last_seen < ?",
                    (started,),
                )
            self.conn.commit()
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            error = f"{type(exc).__name__}: {exc}"
            db.finish_run(self.conn, run_id, _now(), found, kept, self.api_calls, error)
            raise
        db.finish_run(self.conn, run_id, _now(), found, kept, self.api_calls, None)
        return found, kept
