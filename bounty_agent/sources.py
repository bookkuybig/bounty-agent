"""Bounty boards outside GitHub search, polled each cycle.

TaskBounty publishes a JSON API but its listing was empty when this was
written, so the item schema is unknown. Items are therefore tracked by a
hash of their raw JSON and reported whole; tighten this once real data shows up.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMEOUT = 30


def _fetch(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "bounty-agent", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def new_taskbounty_items(url: str, seen_path: Path) -> list[dict[str, Any]]:
    """Return items not seen in earlier cycles. Network errors return []."""
    try:
        payload = _fetch(url)
    except (OSError, ValueError):
        return []
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []

    try:
        seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()

    fresh = []
    for item in items:
        digest = hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            fresh.append(item if isinstance(item, dict) else {"raw": item})
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    return fresh


def _prize_usd(text: str | None) -> int:
    """'$50,000' -> 50000; anything without a dollar figure -> 0."""
    match = re.search(r"\$\s*([\d,]+)", text or "")
    return int(match.group(1).replace(",", "")) if match else 0


def new_mlcontests_items(url: str, seen_path: Path, min_prize: int) -> list[dict[str, Any]]:
    """Open ML competitions on mlcontests.com not seen in earlier cycles.

    The page embeds every listing as JSON in a `data-competitions` attribute.
    The first run only records what is already open, so it does not alert.
    Network or parse errors return [].
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            page = response.read().decode("utf-8", errors="replace")
        match = re.search(r'data-competitions="([^"]*)"', page)
        listings = json.loads(html.unescape(match.group(1))) if match else []
    except (OSError, ValueError):
        return []

    today = datetime.now(timezone.utc).date()
    open_items = []
    for item in listings:
        try:
            deadline = datetime.strptime(item.get("deadline") or "", "%d %b %Y").date()
        except ValueError:
            continue
        prize = _prize_usd(item.get("prize"))
        if deadline >= today and prize >= min_prize and item.get("url"):
            open_items.append(
                {
                    "title": item.get("name"),
                    "url": item["url"],
                    "prize": item.get("prize"),
                    "deadline": item.get("deadline"),
                    "platform": item.get("platform"),
                    "tags": item.get("tags") or [],
                }
            )

    first_run = not seen_path.exists()
    try:
        seen = set(json.loads(seen_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    fresh = [item for item in open_items if item["url"] not in seen]
    seen.update(item["url"] for item in fresh)
    seen_path.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    return [] if first_run else fresh
