"""FastMCP server exposing read-only Sleeper fantasy football tools."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .http import SleeperAPIError
from .scoring import describe as describe_scoring
from .sleeper import (
    LeagueContext,
    Production,
    Sleeper,
    Team,
    best_lineup,
    injury_text,
    is_unavailable,
    nfl_team_ok,
    position_strength_ranks,
    positional_depth,
    r1,
    roster_needs,
)

log = logging.getLogger("sleeper_mcp")

INSTRUCTIONS = """\
Read-only tools for Sleeper fantasy football (NFL). Nothing here can change a league.

Typical flow: get_user or my_leagues (from a Sleeper username) -> league_overview to
see teams and roster_ids -> waiver_wire / trade_context for decisions.

All points are computed under the specific league's scoring_settings. Stats and
projections come from undocumented Sleeper endpoints and can be missing; tools say so
in a `notes` field when that happens. trade_context returns context, not a verdict:
weigh it yourself (team needs, record, playoff odds, bye weeks, injuries, picks).
"""

mcp = FastMCP("sleeper", instructions=INSTRUCTIONS)
api = Sleeper()

READ_ONLY = {"readOnlyHint": True, "openWorldHint": True, "idempotentHint": True}

LeagueId = Annotated[
    str,
    Field(description="Sleeper league ID (numeric string, e.g. '1370086111409340416'). Get it from my_leagues."),
]
RosterId = Annotated[
    int,
    Field(description="Team's roster_id within the league (1..N). league_overview lists them.", ge=1),
]


# ---- helpers ----------------------------------------------------------------


def _ts(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _league_type(settings: dict) -> str:
    return {0: "redraft", 1: "keeper", 2: "dynasty"}.get(settings.get("type"), "unknown")


def _notes(prod: Production) -> list[str]:
    notes = []
    if not prod.has_stats:
        notes.append("No stats available (season not started, or Sleeper's stats endpoint is down).")
    if not prod.has_projections:
        notes.append("No projections available (season over, or Sleeper's projections endpoint is down).")
    if not prod.schedule.available:
        notes.append("NFL schedule unavailable, so bye weeks and opponents are omitted.")
    return notes


async def _my_team(ctx: LeagueContext, my_roster_id: int | None, username: str | None) -> Team | None:
    if my_roster_id is not None:
        return ctx.team(my_roster_id)
    name = username or os.environ.get("SLEEPER_USERNAME")
    if not name:
        return None
    user_id, _ = await api.resolve_user_id(name)
    team = ctx.roster_for_user(user_id)
    if team is None:
        raise SleeperAPIError(f"User '{name}' has no roster in league {ctx.league_id}.")
    return team


def _resolve_list(
    names: list[str] | None, pool: list[str], whose: str
) -> list[str]:
    if not names:
        return []
    out = []
    for q in names:
        hits = api.players.resolve(q, within=pool)
        if not hits:
            raise SleeperAPIError(f"'{q}' is not on {whose}'s roster.")
        if len(hits) > 1:
            opts = ", ".join(api.players.label(h) for h in hits[:5])
            raise SleeperAPIError(f"'{q}' matches several players on {whose}'s roster: {opts}. Be more specific.")
        out.append(hits[0])
    return out


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Map each key to its percentile (0..1) among the values; ties share a rank."""
    if not values:
        return {}
    ordered = sorted(set(values.values()))
    if len(ordered) == 1:
        return {k: (1.0 if v > 0 else 0.0) for k, v in values.items()}
    idx = {v: i / (len(ordered) - 1) for i, v in enumerate(ordered)}
    return {k: idx[v] for k, v in values.items()}


def _lineup_view(filled: dict[str, list[tuple[str, float]]]) -> list[str]:
    out = []
    for slot, rows in filled.items():
        for pid, v in rows:
            out.append(f"{slot}: {api.players.name(pid) if pid else '(empty)'} {r1(v)}")
    return out


def _team_roster_view(t: Team, prod: Production) -> dict[str, list[dict[str, Any]]]:
    starters, ir, taxi = set(t.starters), set(t.reserve), set(t.taxi)
    out: dict[str, list[dict[str, Any]]] = {}
    for pos, pids in positional_depth(t, prod).items():
        cards = []
        for pid in pids:
            tag = "IR" if pid in ir else "taxi" if pid in taxi else "starter" if pid in starters else "bench"
            cards.append(prod.player_card(pid, extra={"slot": tag}))
        out[pos] = cards
    return out


# ---- priority tools -----------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def waiver_wire(
    league_id: LeagueId,
    position: Annotated[
        str | None,
        Field(description="Only show this position: QB, RB, WR, TE, K, DEF (or FLEX for RB/WR/TE). Omit for all."),
    ] = None,
    limit: Annotated[int, Field(description="How many players to return (1-50).", ge=1, le=50)] = 15,
    my_roster_id: Annotated[
        int | None,
        Field(description="Your roster_id, used to flag which of your thin positions each player fills.", ge=1),
    ] = None,
    username: Annotated[
        str | None,
        Field(description="Your Sleeper username, as an alternative to my_roster_id. Defaults to the SLEEPER_USERNAME env var."),
    ] = None,
) -> dict[str, Any]:
    """Rank the best available (unrostered) players in a league.

    Each player is scored by projected points (this week and the next few), recent
    production over the last 3 completed weeks, and how many Sleeper users added them
    in the last 48 hours. All points use THIS league's scoring_settings. Injury status,
    bye week and this week's opponent are included. If you identify your team, the
    result lists your thin positions and which ones each player would help.
    """
    ctx = await api.league_context(league_id)
    prod, trend = await asyncio.gather(api.production(ctx), api.trending_counts(48, 300))
    me = await _my_team(ctx, my_roster_id, username)

    wanted = ctx.relevant_positions
    if position:
        pos = position.upper().strip()
        if pos in ("DST", "D/ST"):
            pos = "DEF"
        filt = {"RB", "WR", "TE"} if pos == "FLEX" else {pos}
        if not filt & wanted:
            raise SleeperAPIError(f"{position} isn't a starting position in this league ({sorted(wanted)}).")
        wanted = filt & wanted

    rostered = ctx.rostered
    db = api.players
    has_signal = set(prod.season_stats) | set(trend)
    for wk in prod.week_proj.values():
        has_signal |= set(wk)

    candidates: list[str] = []
    for pid in has_signal:
        if pid in rostered:
            continue
        p = db.get(pid)
        if not p or not nfl_team_ok(p.get("team")):
            continue
        if not set(db.positions(pid)) & wanted:
            continue
        if p.get("position") != "DEF" and p.get("active") is False:
            continue
        candidates.append(pid)

    ranks = position_strength_ranks(ctx, prod)
    needs = roster_needs(me, ctx, prod, ranks) if me else {}

    # Score within each position so kickers aren't compared to running backs.
    by_pos: dict[str, list[str]] = {}
    for pid in candidates:
        by_pos.setdefault(db.get(pid).get("position") or "?", []).append(pid)
    scored: list[tuple[float, str, dict]] = []
    for pos, pids in by_pos.items():
        fwd = _percentiles({p: prod.value(p) for p in pids})
        rec = _percentiles({p: float(prod.numbers(p).get("recent_avg") or 0) for p in pids})
        trd = _percentiles({p: float(trend.get(p, 0)) for p in pids})
        for p in pids:
            s = 0.5 * fwd[p] + 0.3 * rec[p] + 0.2 * trd[p]
            if pos in ("K", "DEF"):
                s *= 0.85  # streamable; slightly behind skill players at equal percentile
            if is_unavailable(db.get(p)):
                s *= 0.5
            fills = sorted(set(db.positions(p)) & set(needs))
            if fills:
                s += 0.1
            scored.append((s, p, {"fills": fills}))

    scored.sort(key=lambda x: x[0], reverse=True)
    out_players = []
    for s, pid, meta in scored[:limit]:
        extra: dict[str, Any] = {"waiver_score": round(s * 100)}
        if trend.get(pid):
            extra["adds_last_48h"] = trend[pid]
        if me is not None:
            extra["fills_my_needs"] = [f"{pos}: {'; '.join(needs[pos])}" for pos in meta["fills"]] or None
        out_players.append(prod.player_card(pid, extra=extra))

    result: dict[str, Any] = {
        "league": ctx.name,
        "week": ctx.current_week,
        "scoring": describe_scoring(ctx.scoring)["format"],
        "how_ranked": "waiver_score 0-110: 50% projected pts, 30% recent avg (games played), 20% "
        "trending adds, each as a percentile within the player's position; halved if Out/IR; "
        "+10 if it fills one of your needs.",
        "available": out_players,
    }
    s = ctx.settings
    if s.get("waiver_type") == 2:
        result["waivers"] = "FAAB"
        if me is not None and s.get("waiver_budget"):
            result["my_faab_remaining"] = s["waiver_budget"] - (me.waiver_budget_used or 0)
    elif me is not None and me.waiver_position:
        result["my_waiver_priority"] = me.waiver_position
    if me is not None:
        result["my_team"] = me.name
        result["my_needs"] = needs or "No thin positions detected."
        result["my_position_ranks"] = {
            pos: f"{r}/{len(ctx.teams)}" for pos, r in sorted(ranks.get(me.roster_id, {}).items())
        }
    notes = _notes(prod)
    if notes:
        result["notes"] = notes
    return result


@mcp.tool(annotations=READ_ONLY)
async def trade_context(
    league_id: LeagueId,
    my_roster_id: Annotated[int, Field(description="Your roster_id (league_overview lists them).", ge=1)],
    other_roster_id: Annotated[int, Field(description="The trade partner's roster_id.", ge=1)],
    players_out: Annotated[
        list[str] | None,
        Field(description="Players you would send, by name ('Ja'Marr Chase') or Sleeper player ID. Must be on your roster."),
    ] = None,
    players_in: Annotated[
        list[str] | None,
        Field(description="Players you would receive, by name or player ID. Must be on the other roster."),
    ] = None,
) -> dict[str, Any]:
    """Gather everything needed to evaluate a trade between two teams.

    Returns both rosters grouped by position (with each player's season points per
    game, last-3-week production, projections, injury and bye), records and points,
    league-wide positional ranks, roster needs, draft picks each side has acquired or
    traded away, and the league's scoring and roster rules. If players_out/players_in
    are given, also shows each side's best projected lineup before and after the deal.
    This is context for you to reason over; it deliberately does not judge the trade.
    """
    if my_roster_id == other_roster_id:
        raise SleeperAPIError("my_roster_id and other_roster_id must be different teams.")
    ctx = await api.league_context(league_id)
    prod, picks = await asyncio.gather(api.production(ctx), api.traded_picks(league_id))
    me, them = ctx.team(my_roster_id), ctx.team(other_roster_id)
    out_ids = _resolve_list(players_out, me.players, me.name)
    in_ids = _resolve_list(players_in, them.players, them.name)

    ranks = position_strength_ranks(ctx, prod)
    standing = {t.roster_id: i for i, t in enumerate(ctx.standings(), 1)}
    s = ctx.settings
    slots = ctx.starting_slots

    def side(t: Team) -> dict[str, Any]:
        d = ctx.team_summary(t)
        d["standing"] = f"{standing[t.roster_id]} of {len(ctx.teams)}"
        d["position_ranks"] = {pos: f"{r}/{len(ctx.teams)}" for pos, r in sorted(ranks[t.roster_id].items())}
        d["needs"] = roster_needs(t, ctx, prod, ranks) or "No thin positions detected."
        d["roster"] = _team_roster_view(t, prod)
        acquired = [
            f"{p['season']} round {p['round']} (originally {ctx.team_label(p['roster_id'])})"
            for p in picks
            if p.get("owner_id") == t.roster_id and p.get("roster_id") != t.roster_id
        ]
        traded_away = [
            f"{p['season']} round {p['round']} -> now owned by {ctx.team_label(p['owner_id'])}"
            for p in picks
            if p.get("roster_id") == t.roster_id and p.get("owner_id") != t.roster_id
        ]
        d["draft_picks"] = {
            "acquired": acquired or None,
            "traded_away": traded_away or None,
            "note": "Any pick not listed as traded away is still owned by its original team.",
        }
        if s.get("waiver_type") == 2 and s.get("waiver_budget"):
            d["faab_remaining"] = s["waiver_budget"] - (t.waiver_budget_used or 0)
        return d

    result: dict[str, Any] = {
        "league": {
            "name": ctx.name,
            "season": ctx.season,
            "type": _league_type(s),
            "teams": len(ctx.teams),
            "current_week": ctx.current_week,
            "trade_deadline_week": s.get("trade_deadline") or None,
            "playoff_teams": s.get("playoff_teams"),
            "playoffs_start_week": s.get("playoff_week_start"),
            "starting_lineup": slots,
            "bench_slots": ctx.roster_positions.count("BN"),
            "scoring": describe_scoring(ctx.scoring),
        },
        "my_team": side(me),
        "their_team": side(them),
    }

    if out_ids or in_ids:
        value = prod.value
        me_before, _ = best_lineup(slots, me.players, api.players, value)
        th_before, _ = best_lineup(slots, them.players, api.players, value)
        me_after_players = [p for p in me.players if p not in out_ids] + in_ids
        th_after_players = [p for p in them.players if p not in in_ids] + out_ids
        me_after, me_lineup_after = best_lineup(slots, me_after_players, api.players, value)
        th_after, _ = best_lineup(slots, th_after_players, api.players, value)
        roster_size = len(ctx.roster_positions) - ctx.roster_positions.count("IR")

        def counts(pids: list[str]) -> dict[str, int]:
            c: dict[str, int] = {}
            for p in pids:
                pos = api.players.get(p).get("position") or "?"
                c[pos] = c.get(pos, 0) + 1
            return dict(sorted(c.items()))

        proposal: dict[str, Any] = {
            "i_send": [prod.player_card(p) for p in out_ids],
            "i_receive": [prod.player_card(p) for p in in_ids],
            "lineup_value_basis": "Sum of best projected starting lineup per week "
            "(projected avg over the next few weeks; falls back to recent avg).",
            "my_lineup_value": {"before": r1(me_before), "after": r1(me_after), "change": r1(me_after - me_before)},
            "their_lineup_value": {"before": r1(th_before), "after": r1(th_after), "change": r1(th_after - th_before)},
            "my_position_counts": {"before": counts(me.players), "after": counts(me_after_players)},
            "their_position_counts": {"before": counts(them.players), "after": counts(th_after_players)},
            "my_best_lineup_after": _lineup_view(me_lineup_after),
        }
        net = len(in_ids) - len(out_ids)
        if net and roster_size:
            if len(me_after_players) - len(me.reserve) > roster_size:
                proposal["roster_space"] = f"You'd receive {net} more player(s) than you send and would need to drop someone."
            if len(th_after_players) - len(them.reserve) > roster_size:
                proposal["their_roster_space"] = f"They'd receive {-net} more player(s) than they send and would need to drop someone."
        result["proposal"] = proposal

    notes = _notes(prod)
    if s.get("trade_deadline") and ctx.current_week and ctx.current_week > s["trade_deadline"]:
        notes.append(f"The trade deadline (week {s['trade_deadline']}) has passed.")
    if s.get("disable_trades"):
        notes.append("Trades are disabled in this league.")
    if notes:
        result["notes"] = notes
    return result


# ---- supporting tools ---------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
async def get_user(
    username: Annotated[str, Field(description="Sleeper username or user_id.")],
) -> dict[str, Any]:
    """Look up a Sleeper user's user_id and display name from their username."""
    u = await api.user(username)
    if not u:
        raise SleeperAPIError(f"No Sleeper user named '{username}'.")
    return {"user_id": u["user_id"], "username": u.get("username"), "display_name": u.get("display_name")}


@mcp.tool(annotations=READ_ONLY)
async def my_leagues(
    username: Annotated[
        str | None,
        Field(description="Sleeper username or user_id. Defaults to the SLEEPER_USERNAME env var."),
    ] = None,
    season: Annotated[str | None, Field(description="Season year, e.g. '2026'. Defaults to the current NFL season.")] = None,
) -> dict[str, Any]:
    """List a user's NFL leagues for a season, with league_id and their roster_id in each."""
    name = username or os.environ.get("SLEEPER_USERNAME")
    if not name:
        raise SleeperAPIError("Pass a username (or set SLEEPER_USERNAME).")
    user_id, display = await api.resolve_user_id(name)
    state = await api.state()
    season = season or str(state.get("league_season") or state.get("season"))
    leagues = await api.user_leagues(user_id, season)
    rosters = await asyncio.gather(*(api.rosters(lg["league_id"]) for lg in leagues))
    out = []
    for lg, rs in zip(leagues, rosters):
        mine = next((r for r in rs if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or [])), None)
        st = (mine or {}).get("settings") or {}
        out.append(
            {
                "league_id": lg["league_id"],
                "name": lg.get("name"),
                "teams": lg.get("total_rosters"),
                "status": lg.get("status"),
                "type": _league_type(lg.get("settings") or {}),
                "scoring": describe_scoring(lg.get("scoring_settings") or {})["format"],
                "superflex": "SUPER_FLEX" in (lg.get("roster_positions") or []),
                "my_roster_id": mine.get("roster_id") if mine else None,
                "my_record": f"{st.get('wins', 0)}-{st.get('losses', 0)}" + (f"-{st['ties']}" if st.get("ties") else "") if mine else None,
            }
        )
    return {"user": display, "user_id": user_id, "season": season, "leagues": out}


@mcp.tool(annotations=READ_ONLY)
async def league_overview(league_id: LeagueId) -> dict[str, Any]:
    """Standings, every team's roster_id and owner, and the league's rules (scoring, lineup, playoffs)."""
    ctx = await api.league_context(league_id)
    s = ctx.settings
    teams = []
    for i, t in enumerate(ctx.standings(), 1):
        d = ctx.team_summary(t)
        d["rank"] = i
        teams.append(d)
    return {
        "league": ctx.name,
        "league_id": ctx.league_id,
        "season": ctx.season,
        "status": ctx.league.get("status"),
        "type": _league_type(s),
        "current_week": ctx.current_week,
        "starting_lineup": ctx.starting_slots,
        "bench_slots": ctx.roster_positions.count("BN"),
        "ir_slots": s.get("reserve_slots"),
        "scoring": describe_scoring(ctx.scoring),
        "waivers": "FAAB" if s.get("waiver_type") == 2 else "rolling/priority",
        "faab_budget": s.get("waiver_budget") if s.get("waiver_type") == 2 else None,
        "trade_deadline_week": s.get("trade_deadline") or None,
        "playoff_teams": s.get("playoff_teams"),
        "playoffs_start_week": s.get("playoff_week_start"),
        "standings": teams,
    }


@mcp.tool(annotations=READ_ONLY)
async def transactions(
    league_id: LeagueId,
    week: Annotated[
        int | None,
        Field(description="Week (Sleeper calls it 'round') to list. Defaults to the current week.", ge=1, le=18),
    ] = None,
    include_failed: Annotated[bool, Field(description="Also include failed waiver claims.")] = False,
) -> dict[str, Any]:
    """Trades, waiver claims and free-agent adds/drops in a league for one week, newest first."""
    ctx = await api.league_context(league_id)
    week = week or ctx.current_week or int(ctx.state.get("week") or 1)
    rows = await api.transactions(league_id, week)
    db = api.players
    out = []
    for tx in sorted(rows, key=lambda t: t.get("status_updated") or t.get("created") or 0, reverse=True):
        if tx.get("status") == "failed" and not include_failed:
            continue
        item: dict[str, Any] = {
            "type": tx.get("type"),
            "status": tx.get("status"),
            "when": _ts(tx.get("status_updated") or tx.get("created")),
            "teams": [ctx.team_label(r) for r in tx.get("roster_ids") or []],
        }
        if tx.get("adds"):
            item["adds"] = [f"{db.label(p)} -> {ctx.team_label(r)}" for p, r in tx["adds"].items()]
        if tx.get("drops"):
            item["drops"] = [f"{db.label(p)} dropped by {ctx.team_label(r)}" for p, r in tx["drops"].items()]
        bid = (tx.get("settings") or {}).get("waiver_bid")
        if bid is not None:
            item["faab_bid"] = bid
        if tx.get("draft_picks"):
            item["picks"] = [
                f"{p['season']} round {p['round']} (orig. {ctx.team_label(p['roster_id'])}): "
                f"{ctx.team_label(p.get('previous_owner_id'))} -> {ctx.team_label(p.get('owner_id'))}"
                for p in tx["draft_picks"]
            ]
        if tx.get("waiver_budget"):
            item["faab_moved"] = [
                f"${w['amount']}: {ctx.team_label(w['sender'])} -> {ctx.team_label(w['receiver'])}"
                for w in tx["waiver_budget"]
            ]
        out.append(item)
    return {"league": ctx.name, "week": week, "count": len(out), "transactions": out}


@mcp.tool(annotations=READ_ONLY)
async def traded_picks(league_id: LeagueId) -> dict[str, Any]:
    """Every future draft pick that has changed hands in a league, and who owns it now."""
    ctx = await api.league_context(league_id)
    picks = await api.traded_picks(league_id)
    rows = sorted(picks, key=lambda p: (str(p.get("season")), p.get("round", 0), p.get("roster_id", 0)))
    out = [
        {
            "season": p.get("season"),
            "round": p.get("round"),
            "original_team": ctx.team_label(p.get("roster_id")),
            "current_owner": ctx.team_label(p.get("owner_id")),
            "previous_owner": ctx.team_label(p.get("previous_owner_id")),
        }
        for p in rows
        if p.get("owner_id") != p.get("roster_id")
    ]
    return {
        "league": ctx.name,
        "picks": out,
        "note": "Picks not listed belong to their original team.",
    }


@mcp.tool(annotations=READ_ONLY)
async def playoff_bracket(
    league_id: LeagueId,
    bracket: Annotated[
        Literal["winners", "losers"],
        Field(description="'winners' for the championship bracket, 'losers' for the consolation/toilet bowl."),
    ] = "winners",
) -> dict[str, Any]:
    """A league's playoff bracket with team names, results and how each slot is decided."""
    ctx = await api.league_context(league_id)
    rows = await api.bracket(league_id, bracket)
    if not rows:
        return {"league": ctx.name, "bracket": bracket, "matchups": [], "note": "Bracket not set yet."}

    def slot(team: int | None, src: dict | None) -> str:
        if team:
            return ctx.team_label(team)
        if src:
            if "w" in src:
                return f"winner of match {src['w']}"
            if "l" in src:
                return f"loser of match {src['l']}"
        return "TBD"

    out = []
    for m in sorted(rows, key=lambda m: (m.get("r", 0), m.get("m", 0))):
        item = {
            "round": m.get("r"),
            "match": m.get("m"),
            "team_1": slot(m.get("t1"), m.get("t1_from")),
            "team_2": slot(m.get("t2"), m.get("t2_from")),
        }
        if m.get("w"):
            item["winner"] = ctx.team_label(m["w"])
        if m.get("p"):
            item["decides_place"] = m["p"]
        out.append(item)
    return {"league": ctx.name, "bracket": bracket, "playoffs_start_week": ctx.settings.get("playoff_week_start"), "matchups": out}


@mcp.tool(annotations=READ_ONLY)
async def league_drafts(
    league_id: LeagueId,
    include_picks: Annotated[
        bool, Field(description="Include every pick (player, team, round, auction price). Can be long.")
    ] = False,
    draft_id: Annotated[str | None, Field(description="Only this draft (from a previous call).")] = None,
    include_past_seasons: Annotated[
        bool, Field(description="Also follow the league back through previous seasons (useful for keeper/dynasty).")
    ] = False,
) -> dict[str, Any]:
    """All drafts for a league (and optionally its earlier seasons), with picks if requested."""
    await api.players.ensure()
    league_ids = [league_id]
    if include_past_seasons:
        cur = await api.league(league_id)
        while cur.get("previous_league_id") and cur["previous_league_id"] != "0" and len(league_ids) < 10:
            prev = await api.http.get(f"/league/{cur['previous_league_id']}", ttl=3600)
            if not prev:
                break
            league_ids.append(prev["league_id"])
            cur = prev

    out = []
    for lid in league_ids:
        drafts, users = await asyncio.gather(api.league_drafts(lid), api.league_users(lid))
        names = {u["user_id"]: u.get("display_name") or u["user_id"] for u in users}
        for d in drafts:
            if draft_id and d["draft_id"] != draft_id:
                continue
            settings = d.get("settings") or {}
            item: dict[str, Any] = {
                "draft_id": d["draft_id"],
                "league_id": lid,
                "season": d.get("season"),
                "type": d.get("type"),
                "status": d.get("status"),
                "rounds": settings.get("rounds"),
                "start": _ts(d.get("start_time")),
                "order": [
                    f"{slot}. {names.get(uid, uid)}"
                    for uid, slot in sorted((d.get("draft_order") or {}).items(), key=lambda kv: kv[1])
                ]
                or None,
            }
            if include_picks:
                picks = await api.draft_picks(d["draft_id"])
                rows = []
                for pk in sorted(picks, key=lambda p: p.get("pick_no", 0)):
                    meta = pk.get("metadata") or {}
                    row = f"{pk.get('round')}.{pk.get('draft_slot')} (#{pk.get('pick_no')}) {names.get(pk.get('picked_by'), pk.get('picked_by'))}: "
                    pid = str(pk.get("player_id") or "")
                    row += api.players.label(pid) if api.players.get(pid) else f"{meta.get('first_name', '')} {meta.get('last_name', '')} ({meta.get('position', '?')})".strip()
                    if meta.get("amount"):
                        row += f" ${meta['amount']}"
                    if pk.get("is_keeper"):
                        row += " [keeper]"
                    rows.append(row)
                item["picks"] = rows
            out.append(item)
    return {"drafts": out}


@mcp.tool(annotations=READ_ONLY)
async def trending_players(
    kind: Annotated[Literal["add", "drop"], Field(description="'add' for most-added, 'drop' for most-dropped.")] = "add",
    lookback_hours: Annotated[int, Field(description="How far back to count adds/drops (1-168 hours).", ge=1, le=168)] = 24,
    limit: Annotated[int, Field(description="How many players to return (1-100).", ge=1, le=100)] = 25,
    league_id: Annotated[
        str | None,
        Field(description="Optional league ID: marks each player as available or names the team that has them."),
    ] = None,
) -> dict[str, Any]:
    """Players most added or dropped across all Sleeper leagues over a recent window."""
    await api.players.ensure()
    rows = await api.trending(kind, lookback_hours, limit)
    owner: dict[str, str] = {}
    if league_id:
        ctx = await api.league_context(league_id)
        for t in ctx.teams.values():
            for p in t.players:
                owner[p] = ctx.team_label(t.roster_id)
    out = []
    for r in rows:
        pid = str(r["player_id"])
        p = api.players.get(pid)
        item: dict[str, Any] = {
            "name": p.get("full_name", pid),
            "pos": p.get("position"),
            "team": p.get("team") or "FA",
            f"{kind}s": r.get("count"),
        }
        inj = injury_text(p)
        if inj:
            item["injury"] = inj
        if league_id:
            item["in_your_league"] = owner.get(pid, "available")
        out.append(item)
    return {"kind": kind, "lookback_hours": lookback_hours, "players": out}


def main() -> None:
    logging.basicConfig(level=os.environ.get("SLEEPER_MCP_LOG", "WARNING"))
    transport = os.environ.get("SLEEPER_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport=transport,
            host=os.environ.get("SLEEPER_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("SLEEPER_MCP_PORT", "8000")),
        )


if __name__ == "__main__":
    main()
