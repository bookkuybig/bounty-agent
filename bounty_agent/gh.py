"""GitHub API access through the `gh` CLI.

Shelling out to `gh` means the token stays in the user's keyring and never
touches this repo. The tradeoff is process overhead per call, which is
irrelevant at a few dozen calls per cycle.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# The default installer path covers shells whose PATH was not refreshed after
# installing GitHub CLI (scheduled tasks, sandboxed terminals).
_DEFAULT_GH = Path(r"C:\Program Files\GitHub CLI\gh.exe")
GH = shutil.which("gh") or (str(_DEFAULT_GH) if _DEFAULT_GH.exists() else None)
TIMEOUT = 60
# The scheduled task runs under pythonw, which has no console; without this
# flag Windows opens a new console window for every gh call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GhError(RuntimeError):
    pass


def _run(args: list[str]) -> str:
    if GH is None:
        raise GhError("gh CLI not found on PATH; install GitHub CLI and run `gh auth login`")
    try:
        proc = _spawn(args)
    except subprocess.TimeoutExpired:
        # A hung request must fail this one query, not the whole cycle.
        raise GhError(f"gh {' '.join(args)} timed out after {TIMEOUT}s") from None
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _spawn(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GH, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
        creationflags=NO_WINDOW,
    )


def api(endpoint: str, params: dict[str, str | int] | None = None, retries: int = 2) -> Any:
    """GET a REST endpoint and return parsed JSON."""
    args = ["api", "-X", "GET", endpoint, "-H", "Accept: application/vnd.github+json"]
    for key, value in (params or {}).items():
        args += ["-f", f"{key}={value}"]

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return json.loads(_run(args))
        except GhError as exc:
            last = exc
            message = str(exc).lower()
            # Secondary rate limits and transient 5xx are worth a backoff.
            if "rate limit" in message or "502" in message or "503" in message or "timed out" in message:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise GhError(str(last))


def search_issues(query: str, limit: int) -> list[dict[str, Any]]:
    """Search issues, paging until `limit` or results run out."""
    out: list[dict[str, Any]] = []
    per_page = min(100, limit)
    page = 1
    while len(out) < limit:
        payload = api(
            "search/issues",
            {"q": query, "per_page": per_page, "page": page, "advanced_search": "true"},
        )
        items = payload.get("items", [])
        out.extend(items)
        if len(items) < per_page:
            break
        page += 1
        if page > 10:  # GitHub search caps out at 1000 results
            break
    return out[:limit]


def rate_limit() -> dict[str, Any]:
    return api("rate_limit")
