"""Stage 2: arithmetic-only scoring.

The score is expected USD per hour of work:

    score = ranking_amount * p_win / est_hours

Every input is a crude proxy. The value is not precision, it is throwing
away the ~90% of issues that are obviously not worth reading, so that an
LLM (and your attention) only ever sees the remainder.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# "$1,500", "$ 250", "USD 400", "400 USD". Bounties above this are
# almost always a false positive (a funding total, a revenue figure).
_MAX_PLAUSIBLE = 100_000
_MONEY = re.compile(
    r"(?:\$\s?(?P<a>\d[\d,]*(?:\.\d{2})?)|(?:USD|usd)\s?(?P<b>\d[\d,]*)|(?P<c>\d[\d,]*)\s?(?:USD|usd))"
)


def _to_float(raw: str) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    return value if 0 < value <= _MAX_PLAUSIBLE else None


def parse_amount(title: str, labels: list[str], body: str) -> float | None:
    """Pull a USD bounty out of the issue.

    Title and labels are trusted (that is where maintainers put the reward);
    the body is a fallback because it also contains unrelated numbers, so
    only its first match is used rather than its largest.
    """
    for text in (title, " ".join(labels)):
        hits = [
            v
            for m in _MONEY.finditer(text or "")
            if (v := _to_float(m.group("a") or m.group("b") or m.group("c") or "")) is not None
        ]
        if hits:
            return max(hits)

    match = _MONEY.search(body or "")
    if match:
        return _to_float(match.group("a") or match.group("b") or match.group("c") or "")
    return None


def _age_days(timestamp: str | None) -> float:
    if not timestamp:
        return 0.0
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - moment).total_seconds() / 86400.0


def estimate_hours(labels: list[str], body_len: int, cfg: dict[str, Any]) -> float:
    effort = cfg["effort"]
    lowered = {label.lower() for label in labels}
    matches = [hours for name, hours in effort["label_hours"].items() if name.lower() in lowered]
    if matches:
        return float(min(matches))
    if body_len >= effort["long_body_chars"]:
        return float(effort["long_body_hours"])
    return float(effort["default_hours"])


def win_probability(
    competitors: int | None,
    assigned: bool,
    updated_at: str | None,
    stars: int,
    language: str | None,
    cfg: dict[str, Any],
) -> float:
    scoring = cfg["scoring"]
    stack = cfg["stack"]
    crowded = cfg["filter"]["crowded_repo_stars"]

    if competitors is None:
        # Unmeasured. Assume roughly one rival rather than pretending it is clear.
        p = 0.5 * 0.7
    else:
        p = 1.0 / (1.0 + competitors)

    if assigned:
        p *= scoring["assigned_penalty"]

    # An old issue nobody has closed usually means earlier attempts failed.
    # Less glamorous, less contested, and the reward is still on the table.
    if _age_days(updated_at) >= scoring["stale_bonus_days"]:
        p *= scoring["stale_bonus"]

    # Popular repos pull in new rivals continuously, so today's count understates
    # tomorrow's. Scale down rather than exclude.
    if stars and stars >= crowded:
        p *= 0.6

    preferred = [item.lower() for item in stack["preferred"]]
    if preferred and (language or "").lower() not in preferred:
        p *= stack["off_stack_penalty"]

    return max(0.0, min(1.0, p))


def score_issue(record: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in est_hours, p_win and score. Mutates and returns `record`."""
    scoring = cfg["scoring"]
    labels: list[str] = record.get("labels") or []

    hours = estimate_hours(labels, record.get("body_len") or 0, cfg)
    p = win_probability(
        record.get("competitors"),
        bool(record.get("assignee")) and not is_exempt(record, cfg),
        record.get("updated_at"),
        record.get("stars") or 0,
        record.get("language"),
        cfg,
    )

    amount = record.get("amount")
    if amount is None:
        ranking_amount = scoring["assumed_amount"] * scoring["unknown_amount_penalty"]
    else:
        ranking_amount = amount

    record["est_hours"] = hours
    record["p_win"] = round(p, 4)
    record["score"] = round(ranking_amount * p / max(hours, 0.25), 2)
    return record


def is_exempt(record: dict[str, Any], cfg: dict[str, Any]) -> bool:
    exempt = {name.lower() for name in cfg["filter"].get("exempt_repos", [])}
    return (record.get("repo") or "").lower() in exempt


def drop_reason(record: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Hard filters. Returns None when the issue is worth keeping."""
    flt = cfg["filter"]

    # Screened by hand and rejected: never surface again.
    if (record.get("id") or "") in set(flt.get("blocked_issues", [])):
        return "rejected"

    # Farm checks need repo metadata; skip them when the repo lookup failed
    # rather than dropping a real issue on missing data.
    if record.get("is_fork") is not None:
        stars = record.get("stars") or 0
        forks = record.get("forks") or 0
        if flt.get("drop_forks") and record["is_fork"]:
            return "fork"
        if stars < flt.get("min_repo_stars", 0):
            return f"small-repo:{stars}-stars"
        ratio = flt.get("max_fork_star_ratio", 0)
        if ratio and forks > ratio * max(stars, 1):
            return f"farm:{forks}-forks/{stars}-stars"

    exempt = is_exempt(record, cfg)

    taken = {name.lower() for name in flt.get("drop_labels", [])}
    for label in record.get("labels") or []:
        if label.lower() in taken:
            return f"label:{label}"

    if flt["drop_assigned"] and record.get("assignee") and not exempt:
        return f"assigned:{record['assignee']}"

    competitors = record.get("competitors")
    if competitors is not None and competitors > flt["max_competitors"]:
        return f"crowded:{competitors}-rivals"

    comments = record.get("comments") or 0
    if flt.get("max_comments") and comments > flt["max_comments"] and not exempt:
        return f"busy:{comments}-comments"

    amount = record.get("amount")
    if flt["min_amount"] and amount is not None and amount < flt["min_amount"]:
        return f"low:${amount:.0f}"

    max_age = cfg["search"]["max_age_days"]
    if max_age and _age_days(record.get("created_at")) > max_age:
        return "too-old"

    return None


def prefilter_rank(record: dict[str, Any]) -> float:
    """Cheap ordering used to decide which issues earn a timeline lookup.

    Timeline calls cost one request each, so spend them on the issues whose
    reward is largest and whose comment count hints at low activity. Issues
    with no parsed amount are ranked behind every issue that has one —
    otherwise a placeholder value outbids real rewards for the budget.
    """
    comments = record.get("comments") or 0
    amount = record.get("amount")
    if amount is None:
        return 25.0 / (1.0 + comments)
    return amount / (1.0 + comments)
