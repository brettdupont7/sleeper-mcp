"""Local cache of Sleeper's NFL player database.

`/v1/players/nfl` is a large (~15MB raw) dump that Sleeper asks clients to fetch
at most once per day. We download it, keep only the fields the tools use, and
store the slimmed copy on disk. If a refresh fails we keep serving the stale copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from .http import API_BASE, SleeperHTTP

REFRESH_SECONDS = 24 * 60 * 60
CACHE_FILE = "players_nfl.json"

# Fields kept from each player record.
KEEP = (
    "full_name",
    "first_name",
    "last_name",
    "position",
    "fantasy_positions",
    "team",
    "status",
    "injury_status",
    "injury_body_part",
    "age",
    "years_exp",
    "depth_chart_order",
    "search_rank",
    "active",
)

# Injury designations that mean the player won't play this week.
UNAVAILABLE = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "COV"}


def cache_dir() -> Path:
    env = os.environ.get("SLEEPER_MCP_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "sleeper-mcp"


def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", name.lower())
    return re.sub(r"[^a-z0-9]", "", name)


def _slim(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        rec = {k: p.get(k) for k in KEEP if p.get(k) is not None}
        if "full_name" not in rec:
            first, last = p.get("first_name") or "", p.get("last_name") or ""
            rec["full_name"] = f"{first} {last}".strip() or pid
        out[pid] = rec
    return out


class PlayerDB:
    def __init__(self, http: SleeperHTTP, directory: Path | None = None):
        self.http = http
        self.path = (directory or cache_dir()) / CACHE_FILE
        self.players: dict[str, dict[str, Any]] = {}
        self.fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._by_name: dict[str, list[str]] = {}

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600 if self.fetched_at else float("inf")

    async def ensure(self) -> None:
        """Load from disk, refreshing from Sleeper if the copy is over a day old."""
        if self.players and time.time() - self.fetched_at < REFRESH_SECONDS:
            return
        async with self._lock:
            if self.players and time.time() - self.fetched_at < REFRESH_SECONDS:
                return
            if not self.players:
                self._load_disk()
            if time.time() - self.fetched_at >= REFRESH_SECONDS:
                try:
                    await self._refresh()
                except Exception:
                    if not self.players:
                        raise
                    # Stale data beats no data; try again in an hour.
                    self.fetched_at = time.time() - REFRESH_SECONDS + 3600

    def _load_disk(self) -> None:
        try:
            blob = json.loads(self.path.read_text())
            self.players = blob["players"]
            self.fetched_at = float(blob["fetched_at"])
            self._index()
        except (OSError, ValueError, KeyError):
            self.players, self.fetched_at = {}, 0.0

    async def _refresh(self) -> None:
        raw = json.loads(await self.http.get_raw(f"{API_BASE}/players/nfl"))
        self.players = _slim(raw)
        self.fetched_at = time.time()
        self._index()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": self.fetched_at, "players": self.players}))
        tmp.replace(self.path)

    def _index(self) -> None:
        idx: dict[str, list[str]] = {}
        for pid, p in self.players.items():
            idx.setdefault(_norm(p.get("full_name", "")), []).append(pid)
        self._by_name = idx

    # ---- lookups -----------------------------------------------------------

    def get(self, pid: str) -> dict[str, Any]:
        return self.players.get(str(pid), {})

    def name(self, pid: str | None) -> str:
        if not pid:
            return "(empty)"
        p = self.players.get(str(pid))
        return p.get("full_name", str(pid)) if p else f"Unknown player {pid}"

    def positions(self, pid: str) -> list[str]:
        p = self.get(pid)
        return p.get("fantasy_positions") or ([p["position"]] if p.get("position") else [])

    def label(self, pid: str) -> str:
        """'Joe Burrow (QB, CIN)' style label."""
        p = self.get(pid)
        if not p:
            return f"Unknown player {pid}"
        bits = [p.get("position") or "?", p.get("team") or "FA"]
        return f"{p.get('full_name', pid)} ({', '.join(bits)})"

    def resolve(self, query: str, within: list[str] | None = None) -> list[str]:
        """Match a player ID or name to player IDs, optionally restricted to `within`."""
        q = str(query).strip()
        pool = set(within) if within is not None else None
        if q in self.players and (pool is None or q in pool):
            return [q]
        if q.upper() in self.players and (pool is None or q.upper() in pool):
            return [q.upper()]  # team defense like "PIT"
        n = _norm(q)
        if not n:
            return []
        exact = [pid for pid in self._by_name.get(n, []) if pool is None or pid in pool]
        if exact:
            return self._rank(exact)
        candidates = pool if pool is not None else self.players.keys()
        partial = [pid for pid in candidates if n in _norm(self.get(pid).get("full_name", ""))]
        return self._rank(partial)

    def _rank(self, pids: list[str]) -> list[str]:
        def key(pid: str) -> tuple:
            p = self.get(pid)
            return (not p.get("team"), p.get("search_rank") or 10**7)

        return sorted(pids, key=key)
