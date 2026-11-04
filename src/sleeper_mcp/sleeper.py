"""Documented Sleeper endpoints plus the league/player analysis the tools share."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any

from .http import SleeperAPIError, SleeperHTTP
from .players import UNAVAILABLE, PlayerDB
from .scoring import points
from .undocumented import NFL_TEAMS, Schedule, Undocumented

# Cache lifetimes (seconds) for documented endpoints.
TTL = {
    "state": 300,
    "user": 3600,
    "user_leagues": 600,
    "league": 300,
    "users": 600,
    "rosters": 120,
    "matchups": 120,
    "transactions": 120,
    "traded_picks": 600,
    "bracket": 300,
    "drafts": 600,
    "draft_picks": 600,
    "trending": 600,
}

# Lineup slot -> eligible positions. Order matters for greedy filling:
# fill the most restrictive slots first.
SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "DL": {"DL"},
    "LB": {"LB"},
    "DB": {"DB"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "FLEX": {"RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
SLOT_ORDER = list(SLOT_ELIGIBILITY)
NON_STARTING = {"BN", "IR", "TAXI"}
FLEX_POSITIONS_WITH_BACKUP = {"RB", "WR"}
# How flex slots are typically filled, for working out positional needs.
FLEX_SHARE: dict[str, dict[str, float]] = {
    "FLEX": {"RB": 0.5, "WR": 0.5},
    "WRRB_FLEX": {"RB": 0.5, "WR": 0.5},
    "REC_FLEX": {"WR": 1.0},
    "SUPER_FLEX": {"QB": 1.0},
}


def r1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


class Sleeper:
    def __init__(self, http: SleeperHTTP | None = None):
        self.http = http or SleeperHTTP()
        self.players = PlayerDB(self.http)
        self.undoc = Undocumented(self.http)

    # ---- documented endpoints ---------------------------------------------

    async def state(self) -> dict[str, Any]:
        return await self.http.get("/state/nfl", ttl=TTL["state"]) or {}

    async def user(self, username_or_id: str) -> dict[str, Any] | None:
        return await self.http.get(f"/user/{username_or_id.strip()}", ttl=TTL["user"])

    async def user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/user/{user_id}/leagues/nfl/{season}", ttl=TTL["user_leagues"]) or []

    async def league(self, league_id: str) -> dict[str, Any]:
        data = await self.http.get(f"/league/{league_id}", ttl=TTL["league"])
        if not data:
            raise SleeperAPIError(f"League {league_id} not found. Check the league ID (my_leagues lists yours).")
        return data

    async def league_users(self, league_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/users", ttl=TTL["users"]) or []

    async def rosters(self, league_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/rosters", ttl=TTL["rosters"]) or []

    async def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/matchups/{week}", ttl=TTL["matchups"]) or []

    async def transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/transactions/{week}", ttl=TTL["transactions"]) or []

    async def traded_picks(self, league_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/traded_picks", ttl=TTL["traded_picks"]) or []

    async def bracket(self, league_id: str, which: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/{which}_bracket", ttl=TTL["bracket"]) or []

    async def league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/league/{league_id}/drafts", ttl=TTL["drafts"]) or []

    async def draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/draft/{draft_id}/picks", ttl=TTL["draft_picks"]) or []

    async def draft_traded_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return await self.http.get(f"/draft/{draft_id}/traded_picks", ttl=TTL["draft_picks"]) or []

    async def trending(self, kind: str, lookback_hours: int, limit: int) -> list[dict[str, Any]]:
        return (
            await self.http.get(
                f"/players/nfl/trending/{kind}",
                params={"lookback_hours": lookback_hours, "limit": limit},
                ttl=TTL["trending"],
            )
            or []
        )

    # ---- composite loaders --------------------------------------------------

    async def resolve_user_id(self, username_or_id: str) -> tuple[str, str]:
        u = await self.user(username_or_id)
        if not u:
            raise SleeperAPIError(f"No Sleeper user named '{username_or_id}'.")
        return u["user_id"], u.get("display_name") or u.get("username") or username_or_id

    async def league_context(self, league_id: str) -> "LeagueContext":
        league, users, rosters, state, _ = await asyncio.gather(
            self.league(league_id),
            self.league_users(league_id),
            self.rosters(league_id),
            self.state(),
            self.players.ensure(),
        )
        return LeagueContext(league, users, rosters, state)

    async def production(
        self,
        ctx: "LeagueContext",
        recent_weeks: int = 3,
        proj_weeks: int = 3,
    ) -> "Production":
        """Stats/projections/schedule for the league's season, scored under its rules."""
        season = ctx.season
        weeks_done = ctx.completed_weeks
        recent = weeks_done[-recent_weeks:] if recent_weeks else []
        upcoming = ctx.upcoming_weeks(proj_weeks)

        results = await asyncio.gather(
            self.undoc.season_stats(season),
            self.undoc.schedule(season),
            *(self.undoc.week_stats(season, w, final=True) for w in recent),
            *(self.undoc.week_projections(season, w) for w in upcoming),
        )
        season_stats, games = results[0], results[1]
        week_stats = dict(zip(recent, results[2 : 2 + len(recent)]))
        week_proj = dict(zip(upcoming, results[2 + len(recent) :]))
        return Production(ctx, self.players, season_stats, week_stats, week_proj, Schedule(games))

    async def trending_counts(self, lookback_hours: int = 48, limit: int = 300) -> dict[str, int]:
        rows = await self.trending("add", lookback_hours, limit)
        return {str(r["player_id"]): int(r.get("count", 0)) for r in rows if r.get("player_id")}


@dataclass
class Team:
    roster_id: int
    owner_id: str | None
    name: str
    owner: str
    wins: int
    losses: int
    ties: int
    pf: float
    pa: float
    streak: str | None
    players: list[str]
    starters: list[str]
    reserve: list[str]
    taxi: list[str]
    waiver_budget_used: int | None
    waiver_position: int | None
    co_owner_ids: list[str] = field(default_factory=list)

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


class LeagueContext:
    def __init__(self, league: dict, users: list[dict], rosters: list[dict], state: dict):
        self.league = league
        self.state = state
        self.league_id = str(league["league_id"])
        self.name = league.get("name")
        self.season = str(league.get("season"))
        self.settings = league.get("settings") or {}
        self.scoring: dict[str, float] = league.get("scoring_settings") or {}
        self.roster_positions: list[str] = league.get("roster_positions") or []

        by_user = {u["user_id"]: u for u in users}
        self.user_names = {
            uid: (u.get("display_name") or u.get("username") or uid) for uid, u in by_user.items()
        }
        self.teams: dict[int, Team] = {}
        for r in rosters:
            s = r.get("settings") or {}
            owner_id = r.get("owner_id")
            user = by_user.get(owner_id or "", {})
            owner = self.user_names.get(owner_id or "", "(orphaned team)")
            team_name = (user.get("metadata") or {}).get("team_name") or owner
            self.teams[int(r["roster_id"])] = Team(
                roster_id=int(r["roster_id"]),
                owner_id=owner_id,
                name=team_name,
                owner=owner,
                wins=s.get("wins", 0),
                losses=s.get("losses", 0),
                ties=s.get("ties", 0),
                pf=s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
                pa=s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100,
                streak=(r.get("metadata") or {}).get("streak"),
                players=[str(p) for p in (r.get("players") or [])],
                starters=[str(p) for p in (r.get("starters") or []) if p and p != "0"],
                reserve=[str(p) for p in (r.get("reserve") or [])],
                taxi=[str(p) for p in (r.get("taxi") or [])],
                waiver_budget_used=s.get("waiver_budget_used"),
                waiver_position=s.get("waiver_position"),
                co_owner_ids=list(r.get("co_owners") or []),
            )

    # ---- lookups ------------------------------------------------------------

    def team(self, roster_id: int) -> Team:
        t = self.teams.get(int(roster_id))
        if not t:
            ids = ", ".join(f"{t.roster_id}={t.name}" for t in self.teams.values())
            raise SleeperAPIError(f"No roster {roster_id} in this league. Rosters: {ids}")
        return t

    def team_label(self, roster_id: int | None) -> str:
        if roster_id is None:
            return "(unknown)"
        t = self.teams.get(int(roster_id))
        return f"{t.name} ({t.owner})" if t and t.name != t.owner else (t.owner if t else f"roster {roster_id}")

    def roster_for_user(self, user_id: str) -> Team | None:
        for t in self.teams.values():
            if t.owner_id == user_id or user_id in t.co_owner_ids:
                return t
        return None

    @property
    def rostered(self) -> set[str]:
        return {p for t in self.teams.values() for p in t.players}

    @property
    def starting_slots(self) -> list[str]:
        return [s for s in self.roster_positions if s not in NON_STARTING]

    @property
    def relevant_positions(self) -> set[str]:
        out: set[str] = set()
        for s in self.starting_slots:
            out |= SLOT_ELIGIBILITY.get(s, {s})
        return out

    @property
    def is_current_season(self) -> bool:
        return self.season == str(self.state.get("league_season") or self.state.get("season"))

    @property
    def current_week(self) -> int | None:
        """The NFL week in progress for this league's season, or None if over/not started."""
        if not self.is_current_season:
            return None
        if self.state.get("season_type") != "regular":
            return None
        return int(self.state.get("week") or 0) or None

    @property
    def completed_weeks(self) -> list[int]:
        st = self.state.get("season_type")
        if not self.is_current_season:
            return list(range(1, 19)) if self.season < str(self.state.get("season")) else []
        if st == "regular":
            return list(range(1, int(self.state.get("week") or 1)))
        if st == "post":
            return list(range(1, 19))
        return []

    def upcoming_weeks(self, n: int) -> list[int]:
        cw = self.current_week
        if cw is None:
            if self.is_current_season and self.state.get("season_type") in ("pre", "off"):
                return list(range(1, 1 + n))
            return []
        return [w for w in range(cw, cw + n) if w <= 18]

    def team_summary(self, t: Team) -> dict[str, Any]:
        out: dict[str, Any] = {
            "roster_id": t.roster_id,
            "team": t.name,
            "owner": t.owner,
            "record": t.record,
            "points_for": round(t.pf, 1),
            "points_against": round(t.pa, 1),
        }
        if t.streak:
            out["streak"] = t.streak
        return out

    def standings(self) -> list[Team]:
        return sorted(self.teams.values(), key=lambda t: (-t.wins, t.losses, -t.pf))


class Production:
    """Per-player numbers for one league, scored with its scoring_settings."""

    def __init__(
        self,
        ctx: LeagueContext,
        players: PlayerDB,
        season_stats: dict[str, dict],
        week_stats: dict[int, dict[str, dict]],
        week_proj: dict[int, dict[str, dict]],
        schedule: Schedule,
    ):
        self.ctx = ctx
        self.db = players
        self.season_stats = season_stats
        self.week_stats = week_stats
        self.week_proj = week_proj
        self.schedule = schedule
        self.recent_weeks = sorted(week_stats)
        self.proj_weeks = sorted(week_proj)
        self._cache: dict[str, dict[str, Any]] = {}

    @property
    def has_stats(self) -> bool:
        return bool(self.season_stats) or any(self.week_stats.values())

    @property
    def has_projections(self) -> bool:
        return any(self.week_proj.values())

    def numbers(self, pid: str) -> dict[str, Any]:
        """Season, recent and projected league points for one player (memoised)."""
        if pid in self._cache:
            return self._cache[pid]
        sc = self.ctx.scoring
        p = self.db.get(pid)
        team = p.get("team")
        out: dict[str, Any] = {}

        ss = self.season_stats.get(pid)
        gp = int((ss or {}).get("gp") or 0)
        if ss and gp:
            total = points(ss, sc) or 0.0
            out["season_pts"] = r1(total)
            out["games"] = gp
            out["ppg"] = r1(total / gp)

        if self.recent_weeks:
            weekly: dict[str, Any] = {}
            played: list[float] = []
            for w in self.recent_weeks:
                row = self.week_stats[w].get(pid)
                if row and (row.get("gp") or row.get("gms_active")):
                    pts = points(row, sc) or 0.0
                    played.append(pts)
                    weekly[f"wk{w}"] = r1(pts)
                elif self.schedule.opponent(team, w) == "BYE":
                    weekly[f"wk{w}"] = "bye"
                else:
                    weekly[f"wk{w}"] = "DNP"
            out["last_weeks"] = weekly
            # Averaged over games actually played, so a missed game isn't a zero.
            out["recent_avg"] = r1(sum(played) / len(played)) if played else None

        proj_vals = []
        for i, w in enumerate(self.proj_weeks):
            opp = self.schedule.opponent(team, w)
            row = self.week_proj[w].get(pid)
            val = points(row, sc) if row else None
            if opp == "BYE":
                val = 0.0
            if i == 0:
                out["proj_this_week"] = r1(val) if val is not None else None
                if opp:
                    out["opp_this_week"] = opp
            if opp != "BYE" and val is not None:
                proj_vals.append(val)
        if len(self.proj_weeks) > 1:
            out[f"proj_avg_next_{len(self.proj_weeks)}"] = r1(sum(proj_vals) / len(proj_vals)) if proj_vals else None

        bye = self.schedule.bye_week(team)
        if bye:
            out["bye_week"] = bye
        self._cache[pid] = out
        return out

    def value(self, pid: str) -> float:
        """Single forward-looking number used for ranking and lineup building."""
        n = self.numbers(pid)
        for key in (f"proj_avg_next_{len(self.proj_weeks)}", "proj_this_week", "recent_avg", "ppg"):
            v = n.get(key)
            if v is not None:
                return float(v)
        return 0.0

    def this_week_value(self, pid: str) -> float:
        n = self.numbers(pid)
        if n.get("opp_this_week") == "BYE" or is_unavailable(self.db.get(pid)):
            return 0.0
        v = n.get("proj_this_week")
        return float(v) if v is not None else self.value(pid)

    def player_card(self, pid: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        p = self.db.get(pid)
        card: dict[str, Any] = {
            "name": p.get("full_name", f"Unknown player {pid}"),
            "pos": p.get("position"),
            "team": p.get("team") or "FA",
        }
        if p.get("age"):
            card["age"] = p["age"]
        inj = injury_text(p)
        if inj:
            card["injury"] = inj
        card.update(self.numbers(pid))
        if extra:
            card.update(extra)
        return card


# ---- shared helpers ---------------------------------------------------------


def injury_text(p: dict[str, Any]) -> str | None:
    status = p.get("injury_status")
    if not status:
        if p.get("status") and p.get("status") not in ("Active",) and p.get("position") != "DEF":
            return p["status"]
        return None
    part = p.get("injury_body_part")
    return f"{status} ({part})" if part else status


def is_unavailable(p: dict[str, Any]) -> bool:
    return (p.get("injury_status") or "") in UNAVAILABLE


def best_lineup(
    slots: list[str], pids: list[str], db: PlayerDB, value_fn
) -> tuple[float, dict[str, list[tuple[str, float]]]]:
    """Greedy optimal lineup. Returns total and {slot: [(pid, value), ...]}."""
    pool = sorted(pids, key=value_fn, reverse=True)
    used: set[str] = set()
    filled: dict[str, list[tuple[str, float]]] = {}
    total = 0.0
    ordered = sorted(slots, key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else len(SLOT_ORDER))
    for slot in ordered:
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        pick = next((p for p in pool if p not in used and set(db.positions(p)) & elig), None)
        if pick is None:
            filled.setdefault(slot, []).append(("", 0.0))
            continue
        used.add(pick)
        v = value_fn(pick)
        total += v
        filled.setdefault(slot, []).append((pick, v))
    return total, filled


def position_targets(slots: list[str]) -> dict[str, float]:
    """How many players a team needs at each position to field a lineup.

    Dedicated slots count fully. Flex slots are split by how they're usually
    filled (FLEX goes to RB/WR, SUPER_FLEX to QB), not evenly across every
    eligible position, so a 1-TE league doesn't "need" two TEs. RB/WR get one
    extra body for injuries and byes.
    """
    need: dict[str, float] = {}
    for s in slots:
        share = FLEX_SHARE.get(s)
        if share is None:
            elig = SLOT_ELIGIBILITY.get(s, {s})
            share = {pos: 1 / len(elig) for pos in elig}
        for pos, frac in share.items():
            need[pos] = need.get(pos, 0) + frac
    for pos in need:
        if pos in FLEX_POSITIONS_WITH_BACKUP:
            need[pos] += 1
    return {pos: math.floor(v + 0.5) for pos, v in need.items() if v >= 0.5}


def positional_depth(team: Team, prod: Production) -> dict[str, list[str]]:
    """All rostered players grouped by primary position, best projected first."""
    db = prod.db
    out: dict[str, list[str]] = {}
    for pid in team.players:
        p = db.get(pid)
        pos = p.get("position") or (db.positions(pid) or ["?"])[0]
        out.setdefault(pos, []).append(pid)
    for pos in out:
        out[pos].sort(key=prod.value, reverse=True)
    return out


def position_strength_ranks(ctx: LeagueContext, prod: Production) -> dict[int, dict[str, int]]:
    """Rank every team (1 = best) at each starting position by its top-N projected value."""
    slots = ctx.starting_slots
    dedicated: dict[str, int] = {}
    for s in slots:
        elig = SLOT_ELIGIBILITY.get(s, {s})
        if len(elig) == 1:
            pos = next(iter(elig))
            dedicated[pos] = dedicated.get(pos, 0) + 1
    # Count flex usage too, so WR-heavy leagues compare WR3s.
    for s in slots:
        elig = SLOT_ELIGIBILITY.get(s, {s})
        if len(elig) > 1:
            for pos in elig & {"RB", "WR"}:
                dedicated[pos] = dedicated.get(pos, 0) + 0.5
    strength: dict[str, dict[int, float]] = {}
    for rid, t in ctx.teams.items():
        healthy = [p for p in t.players if not is_unavailable(prod.db.get(p))]
        for pos, n in dedicated.items():
            vals = sorted(
                (prod.value(p) for p in healthy if pos in prod.db.positions(p)), reverse=True
            )[: max(1, round(n))]
            strength.setdefault(pos, {})[rid] = sum(vals)
    ranks: dict[int, dict[str, int]] = {rid: {} for rid in ctx.teams}
    for pos, by_team in strength.items():
        ordered = sorted(by_team, key=by_team.get, reverse=True)
        for i, rid in enumerate(ordered, 1):
            ranks[rid][pos] = i
    return ranks


def roster_needs(team: Team, ctx: LeagueContext, prod: Production, ranks: dict[int, dict[str, int]] | None = None) -> dict[str, list[str]]:
    """Positions where this roster is thin, with the reasons why."""
    ranks = ranks if ranks is not None else position_strength_ranks(ctx, prod)
    targets = position_targets(ctx.starting_slots)
    n_teams = len(ctx.teams)
    week = ctx.current_week
    db = prod.db
    needs: dict[str, list[str]] = {}

    for pos, target in targets.items():
        holders = [p for p in team.players if pos in db.positions(p)]
        healthy = [p for p in holders if not is_unavailable(db.get(p))]
        if len(healthy) < target:
            needs.setdefault(pos, []).append(f"only {len(healthy)} healthy {pos} (want {target})")
        rank = ranks.get(team.roster_id, {}).get(pos)
        if rank and n_teams >= 4 and rank > n_teams * 2 / 3:
            needs.setdefault(pos, []).append(f"{pos} group ranks {rank}/{n_teams} in league by projection")
        if week:
            dedicated = sum(1 for s in ctx.starting_slots if SLOT_ELIGIBILITY.get(s, {s}) == {pos})
            playable = [p for p in healthy if prod.numbers(p).get("opp_this_week") not in ("BYE", None) or not prod.schedule.available]
            if dedicated and len(playable) < dedicated:
                needs.setdefault(pos, []).append(f"not enough {pos} with a game in week {week} (byes/injuries)")
    return needs


def nfl_team_ok(team: str | None) -> bool:
    return bool(team) and team in NFL_TEAMS
