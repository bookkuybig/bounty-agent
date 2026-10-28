"""Alerts for newly passing issues: Windows toast and ntfy phone push. Stdlib only."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Issue titles come from strangers on GitHub. They reach PowerShell through
# environment variables and are XML-escaped there, never spliced into the script.
_TOAST = r"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$t = [System.Security.SecurityElement]::Escape($env:BA_TITLE)
$b = [System.Security.SecurityElement]::Escape($env:BA_BODY)
$u = [System.Security.SecurityElement]::Escape($env:BA_URL)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast activationType=`"protocol`" launch=`"$u`"><visual><binding template=`"ToastGeneric`"><text>$t</text><text>$b</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def toast(title: str, body: str, url: str) -> None:
    env = dict(os.environ, BA_TITLE=title[:120], BA_BODY=body[:200], BA_URL=url)
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST],
            env=env,
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass  # an alert that fails to show must never fail the cycle


def push(topic: str, title: str, body: str, url: str) -> None:
    """Phone push through ntfy.sh. The topic name is the only secret: keep it long."""
    # JSON publishing keeps non-ASCII titles intact; HTTP headers would not.
    payload = {"topic": topic, "title": title[:120], "message": body[:300] or title, "click": url}
    request = urllib.request.Request(
        "https://ntfy.sh/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=20).close()
    except OSError:
        pass  # same rule as the toast: never fail the cycle over an alert


def alert(title: str, body: str, url: str, opts: dict[str, Any]) -> None:
    if opts.get("toast"):
        toast(title, body, url)
    if opts.get("ntfy_topic"):
        push(opts["ntfy_topic"], title, body, url)


def announce(hits: list[dict[str, Any]], log_path: Path, opts: dict[str, Any]) -> None:
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as fh:
        for hit in hits:
            amount = f"${hit['amount']:.0f}" if hit.get("amount") is not None else "$?"
            fh.write(f"{when}\t{amount}\t{hit['id']}\t{hit['url']}\t{hit['title']}\n")
    if not hits:
        return
    if len(hits) == 1:
        hit = hits[0]
        amount = f"${hit['amount']:.0f}" if hit.get("amount") is not None else "amount ?"
        alert(f"New bounty: {amount} {hit['repo']}", hit["title"], hit["url"], opts)
    else:
        names = ", ".join(h["id"] for h in hits[:3])
        alert(f"{len(hits)} new bounties", names, hits[0]["url"], opts)


def announce_raw(source: str, items: list[dict[str, Any]], log_path: Path, opts: dict[str, Any]) -> None:
    """Alert for boards whose item schema is not known yet: log each item whole."""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as fh:
        for item in items:
            fh.write(f"{when}\t{source}\t{json.dumps(item, ensure_ascii=False)}\n")
    if items:
        first = items[0]
        label = str(first.get("title") or first.get("name") or "see alerts.log")
        url = str(first.get("url") or first.get("link") or "https://www.task-bounty.com")
        alert(f"{len(items)} new {source} item(s)", label, url, opts)

