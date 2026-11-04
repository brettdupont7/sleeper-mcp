"""Undocumented Sleeper endpoints: stats, projections, and the NFL schedule.

None of these are in https://docs.sleeper.com. Sleeper's own app uses them, but
they can change shape or disappear without notice, so every call goes through
this module and fails soft: a broken endpoint returns empty data and the tools
keep working, just without that signal.
"""

from __future__ import annotations

import logging
from typing import Any

from .http import ALT_API_BASE, SleeperHTTP

log = logging.getLogger(__name__)

# Completed weeks rarely change (stat corrections aside); live weeks do.
TTL_FINAL = 12 * 3600
TTL_LIVE = 10 * 60
TTL_PROJ = 60 * 60
TTL_SCHEDULE = 12 * 3600

NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET",
    "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE",
    "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}


def _clean(payload: Any) -> dict[str, dict[str, float]]:
    """Normalise to {player_id: {stat: value}}, dropping the TEAM_XXX duplicates."""
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if isinstance(v, dict) and not k.startswith("TEAM_")}
    if isinstance(payload, list):  # newer list-of-rows shape, just in case
        out = {}
        for row in payload:
            if isinstance(row, dict) and row.get("player_id") and isinstance(row.get("stats"), dict):
                out[str(row["player_id"])] = row["stats"]
        return out
    return {}


class Undocumented:
    def __init__(self, http: SleeperHTTP):
        self.http = http

    async def _safe(self, path: str, ttl: float, base: str | None = None) -> Any:
        try:
            if base:
                return await self.http.get(path, ttl=ttl, base=base)
            return await self.http.get(path, ttl=ttl)
        except Exception as exc:  # undocumented: never let these break a tool
            log.warning("undocumented endpoint %s failed: %s", path, exc)
            return None

    async def week_stats(
        self, season: str, week: int, *, final: bool = True, season_type: str = "regular"
    ) -> dict[str, dict[str, float]]:
        data = await self._safe(
            f"/stats/nfl/{season_type}/{season}/{week}", TTL_FINAL if final else TTL_LIVE
        )
        return _clean(data)

    async def season_stats(self, season: str, season_type: str = "regular") -> dict[str, dict[str, float]]:
        """Season-to-date totals per player (includes `gp`, games played)."""
        return _clean(await self._safe(f"/stats/nfl/{season_type}/{season}", TTL_LIVE * 3))

    async def week_projections(
        self, season: str, week: int, season_type: str = "regular"
    ) -> dict[str, dict[str, float]]:
        return _clean(await self._safe(f"/projections/nfl/{season_type}/{season}/{week}", TTL_PROJ))

    async def schedule(self, season: str, season_type: str = "regular") -> list[dict[str, Any]]:
        data = await self._safe(f"/schedule/nfl/{season_type}/{season}", TTL_SCHEDULE, base=ALT_API_BASE)
        return data if isinstance(data, list) else []


class Schedule:
    """Bye weeks and opponents derived from the season schedule."""

    def __init__(self, games: list[dict[str, Any]]):
        self.opp: dict[tuple[str, int], str] = {}
        self.status: dict[tuple[str, int], str] = {}
        weeks: set[int] = set()
        for g in games:
            wk, home, away = g.get("week"), g.get("home"), g.get("away")
            if not (isinstance(wk, int) and home and away):
                continue
            weeks.add(wk)
            self.opp[(home, wk)] = f"vs {away}"
            self.opp[(away, wk)] = f"@ {home}"
            self.status[(home, wk)] = self.status[(away, wk)] = g.get("status") or ""
        self.weeks = sorted(weeks)
        self.available = bool(self.opp)

    def opponent(self, team: str | None, week: int) -> str | None:
        if not team or not self.available:
            return None
        return self.opp.get((team, week), "BYE" if week in self.weeks else None)

    def game_started(self, team: str | None, week: int) -> bool:
        """True once a team's game that week has kicked off (players are locked)."""
        st = self.status.get((team or "", week), "")
        return bool(st) and st not in ("pre_game", "scheduled", "")

    def bye_week(self, team: str | None) -> int | None:
        if not team or not self.available:
            return None
        for wk in self.weeks:
            if (team, wk) not in self.opp:
                return wk
        return None
