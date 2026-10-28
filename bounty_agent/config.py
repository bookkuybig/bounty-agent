"""Config loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
DB_PATH = ROOT / "bounties.db"
LOG_PATH = ROOT / "collect.log"
ALERTS_PATH = ROOT / "alerts.log"
TASKBOUNTY_SEEN_PATH = ROOT / "taskbounty_seen.json"
MLCONTESTS_SEEN_PATH = ROOT / "mlcontests_seen.json"


def load(path: Path | None = None) -> dict[str, Any]:
    target = path or CONFIG_PATH
    with target.open("rb") as fh:
        return tomllib.load(fh)
