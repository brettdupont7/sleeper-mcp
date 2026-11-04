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
    injury_text,
    is_unavailable,
    position_strength_ranks,
    position_targets,
    positional_depth,
    r1,
    roster_needs,
)
from .season import DEFAULT_SIMS, SeasonSim, SimResult, lineup_check, pairings, team_projection, win_probability
from .trade import DEFAULT_MAX_PARTNER_LOSS, REPLACEMENT_DEPTH, Valuation, free_agents

log = logging.getLogger("sleeper_mcp")

INSTRUCTIONS = """\
Read-only tools for Sleeper fantasy football (NFL). Nothing here can change a league.

Typical flow: get_user or my_leagues (from a Sleeper username) -> league_overview to
see teams and roster_ids -> waiver_wire for pickups, find_trades to discover trade
ideas, trade_context to dig into a specific trade, my_lineup for weekly start/sit,
playoff_odds to see who's contending.

All points are computed under the specific league's scoring_settings. Stats and
projections come from undocumented Sleeper endpoints and can be missing; tools say so
in a `notes` field when that happens. `vs_replacement` is projected points per game
above the best free agents at that position in this league, the fairest single number
for comparing players across positions. trade_context and find_trades return context,
not a verdict: weigh it yourself (record, playoff odds, injuries, picks, the manager).
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


def _resolve_anywhere(names: list[str] | None, ctx: LeagueContext, me: Team) -> set[str]:
    """Resolve names against your roster first, then every roster in the league."""
    out: set[str] = set()
    for q in names or []:
        hits = api.players.resolve(q, within=me.players)
        if not hits:
            hits = api.players.resolve(q, within=sorted(ctx.rostered))
        if not hits:
            raise SleeperAPIError(f"'{q}' isn't on any roster in this league.")
        if len(hits) > 1:
            opts = ", ".join(api.players.label(h) for h in hits[:5])
            raise SleeperAPIError(f"'{q}' matches several rostered players: {opts}. Be more specific.")
        out.add(hits[0])
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


def _team_roster_view(t: Team, prod: Production, val: Valuation) -> dict[str, list[dict[str, Any]]]:
    starters, ir, taxi = set(t.starters), set(t.reserve), set(t.taxi)
    out: dict[str, list[dict[str, Any]]] = {}
    for pos, pids in positional_depth(t, prod).items():
        cards = []
        for pid in pids:
            tag = "IR" if pid in ir else "taxi" if pid in taxi else "starter" if pid in starters else "bench"
            cards.append(prod.player_card(pid, extra={"slot": tag, "vs_replacement": r1(val.vor(pid))}))
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

    db = api.players
    candidates = free_agents(ctx, prod, wanted)

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

    Returns both rosters grouped by position. Each player has points per game,
    last-3-week production, projections (next 3 weeks and rest of season), injury,
    bye, and `vs_replacement`: projected points per game above the best free agents
    at his position in this league. Also returns records, standings, league-wide
    positional ranks, roster needs, traded draft picks, and the scoring, lineup and
    playoff rules.

    If players_out/players_in are given, `proposal` simulates both teams' best
    lineups week by week through the fantasy playoffs, before and after the trade.
    Empty lineup slots (byes, injuries) are filled with a replacement-level free
    agent rather than a zero. It reports the per-week change, rest-of-season and
    playoff-week totals, and the weeks each team would need a waiver starter.

    This is context, not a verdict. Weigh it with things the numbers can't see:
    record and playoff odds, risk tolerance, injury outlook, and the other manager.

    Each team also gets a `season_outlook` (playoff and title odds from a season
    simulation), and a proposal gets `playoff_odds_change` for both sides.
    """
    if my_roster_id == other_roster_id:
        raise SleeperAPIError("my_roster_id and other_roster_id must be different teams.")
    ctx = await api.league_context(league_id)
    prod, picks = await asyncio.gather(api.production(ctx, rest_of_season=True), api.traded_picks(league_id))
    me, them = ctx.team(my_roster_id), ctx.team(other_roster_id)
    out_ids = _resolve_list(players_out, me.players, me.name)
    in_ids = _resolve_list(players_in, them.players, them.name)

    val = Valuation(ctx, prod)
    ranks = position_strength_ranks(ctx, prod)
    standing = {t.roster_id: i for i, t in enumerate(ctx.standings(), 1)}
    s = ctx.settings
    sim = await _season_sim(ctx, val)
    odds = await asyncio.to_thread(sim.run, TRADE_SIMS) if sim.possible else None

    def side(t: Team) -> dict[str, Any]:
        d = ctx.team_summary(t)
        d["standing"] = f"{standing[t.roster_id]} of {len(ctx.teams)}"
        d["position_ranks"] = {pos: f"{r}/{len(ctx.teams)}" for pos, r in sorted(ranks[t.roster_id].items())}
        d["needs"] = roster_needs(t, ctx, prod, ranks) or "No thin positions detected."
        d["roster"] = _team_roster_view(t, prod, val)
        d["draft_picks"] = _pick_summary(ctx, picks, t)
        if odds is not None:
            rid = t.roster_id
            d["season_outlook"] = {
                "playoffs": f"{round(100 * odds.playoffs[rid])}%",
                "champion": f"{round(100 * odds.champion[rid])}%",
                "proj_final_wins": r1(odds.wins[rid]),
            }
        if s.get("waiver_type") == 2 and s.get("waiver_budget"):
            d["faab_remaining"] = s["waiver_budget"] - (t.waiver_budget_used or 0)
        return d

    result: dict[str, Any] = {
        "league": _league_rules(ctx, val),
        "my_team": side(me),
        "their_team": side(them),
    }

    if out_ids or in_ids:
        my_after = [p for p in me.players if p not in out_ids] + in_ids
        their_after = [p for p in them.players if p not in in_ids] + out_ids
        proposal: dict[str, Any] = {
            "i_send": [_trade_card(prod, val, p) for p in out_ids],
            "i_receive": [_trade_card(prod, val, p) for p in in_ids],
            "vs_replacement_sent": r1(sum(val.vor(p) for p in out_ids)),
            "vs_replacement_received": r1(sum(val.vor(p) for p in in_ids)),
            "how_lineups_are_measured": LINEUP_METHOD,
            "my_lineup_change": val.compare(me.players, my_after),
            "their_lineup_change": val.compare(them.players, their_after),
            "my_depth": _depth(ctx, prod, me.players, my_after),
            "their_depth": _depth(ctx, prod, them.players, their_after),
        }
        if odds is not None:
            after = await asyncio.to_thread(
                sim.run, TRADE_SIMS, rosters={me.roster_id: my_after, them.roster_id: their_after}
            )
            proposal["playoff_odds_change"] = {
                "mine": _odds_change(odds, after, me.roster_id),
                "theirs": _odds_change(odds, after, them.roster_id),
                "note": f"From {TRADE_SIMS} simulated seasons each, before and after, with the same "
                "simulated luck, so the difference is the trade. Changes of 1-2 points are noise.",
            }
        proposal.update(_roster_space(ctx, me, them, my_after, their_after))
        result["proposal"] = proposal

    result["notes"] = _trade_notes(ctx, prod) or None
    return result


@mcp.tool(annotations=READ_ONLY)
async def find_trades(
    league_id: LeagueId,
    my_roster_id: Annotated[
        int | None, Field(description="Your roster_id. Or pass username.", ge=1)
    ] = None,
    username: Annotated[
        str | None,
        Field(description="Your Sleeper username, as an alternative to my_roster_id. Defaults to the SLEEPER_USERNAME env var."),
    ] = None,
    target_position: Annotated[
        str | None,
        Field(description="Only suggest trades where you receive this position (QB, RB, WR or TE). Omit to consider all."),
    ] = None,
    partner_roster_id: Annotated[
        int | None, Field(description="Only look at trades with this team.", ge=1)
    ] = None,
    max_players_per_side: Annotated[
        int, Field(description="1 for 1-for-1 trades only; 2 also allows 2-for-1 and 1-for-2.", ge=1, le=2)
    ] = 2,
    limit: Annotated[int, Field(description="How many trade ideas to return per list (1-15).", ge=1, le=15)] = 8,
    max_partner_loss: Annotated[
        float | None,
        Field(
            description="Most lineup points per week a one-sided trade may cost the other team. "
            "Default 2: small asks a sweetener could cover, listed only when win-win trades are scarce "
            "and ranked closest-to-fair first. Raise it (e.g. 5) or pass null for no limit to always "
            "list the biggest upgrades for you, ranked purely by your gain.",
            ge=0,
        ),
    ] = DEFAULT_MAX_PARTNER_LOSS,
    exclude_players: Annotated[
        list[str] | None,
        Field(
            description="Players to leave out of every idea, by name or player ID: your untouchables, "
            "players you've already decided to keep, or players on other teams you don't want."
        ),
    ] = None,
    must_send: Annotated[
        list[str] | None,
        Field(
            description="Players from your roster that every idea must include on your side, by name or "
            "player ID (e.g. ['Bijan Robinson'] to shop him). At most max_players_per_side players."
        ),
    ] = None,
) -> dict[str, Any]:
    """Search every other roster for trades that improve your starting lineup.

    Tries 1-for-1, 2-for-1 and 1-for-2 swaps of players above replacement level
    (kickers, defenses and IR players are skipped). Each candidate is screened on a
    typical week, then the best are simulated week by week through the fantasy
    playoffs, with byes, injuries and waiver streaming accounted for. Only trades
    where both teams' rest-of-season lineup projection goes up are returned in
    `win_win`, sorted by how much they help you. `one_sided` lists trades that help
    you but cost the partner, up to `max_partner_loss` points per week.

    Use exclude_players for players you won't trade (or don't want), and must_send
    to shop a specific player. Without them, a low-value bench player you'd never
    actually move can dominate the ideas.

    Win-win ideas are realistic offers: the other team gains too. One-sided ideas
    show what an upgrade would take; their_gain tells you how big a sweetener it
    needs, and vs_replacement shows whether it looks lopsided on paper. Run
    trade_context on a promising idea for full rosters and context.
    """
    ctx = await api.league_context(league_id)
    prod = await api.production(ctx, rest_of_season=True)
    me = await _my_team(ctx, my_roster_id, username)
    if me is None:
        raise SleeperAPIError("Tell me which team is yours: pass my_roster_id or username (or set SLEEPER_USERNAME).")

    recv: set[str] | None = None
    if target_position:
        pos = target_position.upper().strip()
        if pos not in {"QB", "RB", "WR", "TE"}:
            raise SleeperAPIError("target_position must be QB, RB, WR or TE.")
        recv = {pos}
    if partner_roster_id is not None:
        partners = [ctx.team(partner_roster_id)]
        if partners[0].roster_id == me.roster_id:
            raise SleeperAPIError("partner_roster_id is your own team.")
    else:
        partners = [t for t in ctx.teams.values() if t.roster_id != me.roster_id and t.owner_id]

    excluded = _resolve_anywhere(exclude_players, ctx, me)
    forced = _resolve_list(must_send, me.players, me.name)
    if len(forced) > max_players_per_side:
        raise SleeperAPIError(
            f"must_send lists {len(forced)} players but max_players_per_side is {max_players_per_side}."
        )
    clash = set(forced) & excluded
    if clash:
        names = ", ".join(api.players.name(p) for p in clash)
        raise SleeperAPIError(f"{names} is in both must_send and exclude_players.")

    val = Valuation(ctx, prod)
    win_win, one_sided = await asyncio.to_thread(
        val.search,
        me,
        partners,
        max_per_side=max_players_per_side,
        receive_positions=recv,
        max_partner_loss=max_partner_loss,
        exclude=excluded,
        must_send=forced,
    )
    loose = max_partner_loss is None or max_partner_loss > DEFAULT_MAX_PARTNER_LOSS
    ranks = position_strength_ranks(ctx, prod)
    needs = roster_needs(me, ctx, prod, ranks)

    def render(f: dict) -> dict[str, Any]:
        t: Team = f["partner"]
        mine, theirs = f["mine"], f["theirs"]
        idea: dict[str, Any] = {
            "partner": ctx.team_label(t.roster_id),
            "partner_roster_id": t.roster_id,
            "partner_record": t.record,
            "you_send": [_trade_card(prod, val, p, brief=True) for p in f["give"]],
            "you_receive": [_trade_card(prod, val, p, brief=True) for p in f["get"]],
            "your_gain": _gain_summary(mine),
            "their_gain": _gain_summary(theirs),
            "vs_replacement_sent": r1(f["vor_sent"]),
            "vs_replacement_received": r1(f["vor_received"]),
        }
        recv_pos = {api.players.get(p).get("position") for p in f["get"]}
        filled = sorted(recv_pos & set(needs))
        if filled:
            idea["addresses_my_needs"] = filled
        theirs_needs = roster_needs(t, ctx, prod, ranks)
        sent_pos = {api.players.get(p).get("position") for p in f["give"]}
        if sorted(sent_pos & set(theirs_needs)):
            idea["addresses_their_needs"] = sorted(sent_pos & set(theirs_needs))
        idea.update(_roster_space(ctx, me, t, [p for p in me.players if p not in f["give"]] + f["get"],
                                  [p for p in t.players if p not in f["get"]] + f["give"]))
        return idea

    result: dict[str, Any] = {
        "league": ctx.name,
        "my_team": me.name,
        "my_needs": needs or "No thin positions detected.",
        "weeks_simulated": _weeks_label(val),
        "how_measured": LINEUP_METHOD,
        "filters": {
            "excluded": [api.players.label(p) for p in excluded] or None,
            "must_send": [api.players.label(p) for p in forced] or None,
        },
        "win_win": [render(f) for f in win_win[:limit]]
        or "No trade improves both lineups right now. That's common when your bench has few "
        "players who would start elsewhere.",
    }
    limit_text = "no limit" if max_partner_loss is None else f"up to {r1(max_partner_loss)} pts/week"
    if loose and one_sided:
        result["one_sided"] = [render(f) for f in one_sided[:limit]]
        result["one_sided_note"] = (
            f"These help you and cost the other team ({limit_text}), ranked by your gain. "
            "Expect pushback unless you add a sweetener or the other manager values players differently."
        )
    elif one_sided and len(win_win) < limit:
        result["one_sided"] = [render(f) for f in one_sided[: max(3, limit - len(win_win))]]
        result["one_sided_note"] = (
            f"These help you but cost the other team a little ({limit_text}), closest to fair first. "
            "They'd likely need a sweetener: a bench player, a draft pick, or FAAB. "
            "Raise max_partner_loss to see bigger, more lopsided upgrades."
        )
    elif not one_sided and len(win_win) < limit:
        msg = f"No trades help you by at least 1 point per week while costing the other team {limit_text}."
        if forced:
            sent = " + ".join(api.players.name(p) for p in forced)
            msg += f" Moving {sent} for a lineup upgrade usually costs the other side more than that."
        if max_partner_loss is not None:
            msg += " Raise max_partner_loss (e.g. 3-5) to see what it would take."
        result["one_sided"] = msg
    notes = _trade_notes(ctx, prod)
    if notes:
        result["notes"] = notes
    return result


TRADE_SIMS = 3000

LINEUP_METHOD = (
    "Best starting lineup each week from projections under this league's scoring. "
    "Byes and Out/IR count as zero for that week, and any slot the roster can't fill "
    "better than the waiver wire is filled with a replacement-level free agent "
    f"(average of the top {REPLACEMENT_DEPTH} available at that position)."
)


def _weeks_label(val: Valuation) -> str:
    if val.weeks == [None]:
        return "one typical week (no weekly projections available)"
    w = [x for x in val.weeks if x is not None]
    label = f"weeks {w[0]}-{w[-1]}" if len(w) > 1 else f"week {w[0]}"
    if val.playoff_weeks:
        label += f" (playoffs: weeks {val.playoff_weeks[0]}-{val.playoff_weeks[-1]})"
    return label


def _gain_summary(cmp: dict[str, Any]) -> dict[str, Any]:
    out = {"rest_of_season_total": cmp["rest_of_season_total"], "avg_per_week": cmp["avg_per_week"]}
    if "playoff_weeks_total" in cmp:
        out["playoff_weeks_total"] = cmp["playoff_weeks_total"]
    return out


def _trade_card(prod: Production, val: Valuation, pid: str, brief: bool = False) -> dict[str, Any]:
    extra = {"vs_replacement": r1(val.vor(pid))}
    if not brief:
        return prod.player_card(pid, extra=extra)
    p = api.players.get(pid)
    n = prod.numbers(pid)
    card: dict[str, Any] = {"name": p.get("full_name", pid), "pos": p.get("position"), "team": p.get("team") or "FA"}
    inj = injury_text(p)
    if inj:
        card["injury"] = inj
    for k in ("ppg", "recent_avg", "proj_avg_rest_of_season", "bye_week"):
        if n.get(k) is not None:
            card[k] = n[k]
    card.update(extra)
    return card


def _league_rules(ctx: LeagueContext, val: Valuation) -> dict[str, Any]:
    s = ctx.settings
    return {
        "name": ctx.name,
        "season": ctx.season,
        "type": _league_type(s),
        "teams": len(ctx.teams),
        "current_week": ctx.current_week,
        "trade_deadline_week": s.get("trade_deadline") or None,
        "playoff_teams": s.get("playoff_teams"),
        "playoff_weeks": ctx.playoff_weeks or None,
        "starting_lineup": ctx.starting_slots,
        "bench_slots": ctx.roster_positions.count("BN"),
        "scoring": describe_scoring(ctx.scoring),
        "replacement_level_ppg": {
            pos: r1(v) for pos, v in sorted(val.replacement_ros.items()) if pos in ctx.relevant_positions
        },
    }


def _pick_summary(ctx: LeagueContext, picks: list[dict], t: Team) -> dict[str, Any]:
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
    return {
        "acquired": acquired or None,
        "traded_away": traded_away or None,
        "note": "Any pick not listed as traded away is still owned by its original team.",
    }


def _depth(ctx: LeagueContext, prod: Production, before: list[str], after: list[str]) -> dict[str, str]:
    """Healthy players per starting position vs. what a lineup needs, where it changes."""
    targets = position_targets(ctx.starting_slots)
    db = api.players

    def healthy(pids: list[str], pos: str) -> int:
        return sum(1 for p in pids if pos in db.positions(p) and not is_unavailable(db.get(p)))

    out = {}
    for pos, want in targets.items():
        b, a = healthy(before, pos), healthy(after, pos)
        if a != b:
            flag = " (below target)" if a < want else ""
            out[pos] = f"{b} -> {a} healthy (target {want}){flag}"
    return out or {"unchanged": "No change in healthy players at any starting position."}


def _roster_space(ctx: LeagueContext, me: Team, them: Team, my_after: list[str], their_after: list[str]) -> dict[str, str]:
    size = len(ctx.roster_positions) - ctx.roster_positions.count("IR")
    out = {}
    if not size:
        return out
    mine_over = len(my_after) - len(me.reserve) - size
    theirs_over = len(their_after) - len(them.reserve) - size
    if mine_over > 0:
        out["my_roster_space"] = f"You'd need to drop {mine_over} player(s) to make room."
    if theirs_over > 0:
        out["their_roster_space"] = f"They'd need to drop {theirs_over} player(s) to make room."
    return out


def _trade_notes(ctx: LeagueContext, prod: Production) -> list[str]:
    s = ctx.settings
    notes = _notes(prod)
    if s.get("trade_deadline") and ctx.current_week and ctx.current_week > s["trade_deadline"]:
        notes.append(f"The trade deadline (week {s['trade_deadline']}) has passed.")
    if s.get("disable_trades"):
        notes.append("Trades are disabled in this league.")
    if _league_type(s) in ("keeper", "dynasty"):
        notes.append("This is a keeper/dynasty league: values here cover this season only; age and future value aren't modeled.")
    return notes


@mcp.tool(annotations=READ_ONLY)
async def my_lineup(
    league_id: LeagueId,
    my_roster_id: Annotated[int | None, Field(description="Your roster_id. Or pass username.", ge=1)] = None,
    username: Annotated[
        str | None,
        Field(description="Your Sleeper username, as an alternative to my_roster_id. Defaults to the SLEEPER_USERNAME env var."),
    ] = None,
    week: Annotated[
        int | None,
        Field(description="Week to check. Defaults to the current week. Later weeks use your current lineup and roster.", ge=1, le=18),
    ] = None,
) -> dict[str, Any]:
    """Check your starting lineup for a week (start/sit).

    Shows your current lineup with each starter's projection under this league's
    scoring, opponent, and problems: byes, Out/IR, empty slots. Compares it with
    the best projected lineup from your roster and lists the swaps that gain
    points. Players whose games have kicked off are locked and never moved.
    Also lists free agents for empty slots, or where one projects 3+ points
    better, plus Questionable/Doubtful starters to watch, and this week's
    matchup with both projections and a win probability.
    """
    ctx = await api.league_context(league_id)
    me = await _my_team(ctx, my_roster_id, username)
    if me is None:
        raise SleeperAPIError("Tell me which team is yours: pass my_roster_id or username (or set SLEEPER_USERNAME).")
    week = week or ctx.current_week
    if not week:
        raise SleeperAPIError("The NFL regular season isn't in progress, so there's no lineup to check.")
    if ctx.current_week and week < ctx.current_week:
        raise SleeperAPIError(f"Week {week} is already over; the current week is {ctx.current_week}.")
    prod, matchups = await asyncio.gather(api.production(ctx, rest_of_season=True), api.matchups(league_id, week))
    if week not in prod.week_proj:
        raise SleeperAPIError(f"No projections available for week {week}.")
    val = Valuation(ctx, prod)
    opp_id = next((b if a == me.roster_id else a for a, b in pairings(matchups) if me.roster_id in (a, b)), None)
    result = lineup_check(ctx, prod, val, me, week, ctx.team(opp_id) if opp_id else None)
    result = {"league": ctx.name, "team": me.name, **result}
    notes = _notes(prod)
    if notes:
        result["notes"] = notes
    return result


@mcp.tool(annotations=READ_ONLY)
async def playoff_odds(
    league_id: LeagueId,
    my_roster_id: Annotated[
        int | None, Field(description="Your roster_id, to also get your remaining schedule with win odds.", ge=1)
    ] = None,
    username: Annotated[
        str | None,
        Field(description="Your Sleeper username, as an alternative to my_roster_id. Defaults to the SLEEPER_USERNAME env var."),
    ] = None,
    simulations: Annotated[
        int, Field(description="Number of simulated seasons (1000-20000). More is steadier but slower.", ge=1000, le=20000)
    ] = DEFAULT_SIMS,
) -> dict[str, Any]:
    """Simulate the rest of the season to estimate each team's playoff and title odds.

    Plays out the league's real remaining schedule thousands of times. Each
    team's weekly score is random around its best projected lineup under this
    league's scoring (byes, injuries and waiver streaming included). Seeds use
    wins, then points for. The fantasy playoffs are played as a seeded bracket
    with byes for the top seeds. Returns, per team: projected final wins, playoff
    %, bye %, #1 seed %, title % and average finish. With your team, it also shows
    each remaining regular-season matchup with your win probability.

    Use it to decide whether to buy (contender) or sell (long shot), and to weigh
    trades by what they do to playoff weeks.
    """
    ctx = await api.league_context(league_id)
    prod = await api.production(ctx, rest_of_season=True)
    val = Valuation(ctx, prod)
    sim = await _season_sim(ctx, val)
    if not sim.possible:
        return {
            "league": ctx.name,
            "note": "Nothing to simulate: the regular season is over or hasn't started, "
            "or projections aren't available. See playoff_bracket for the playoffs.",
        }
    me = await _my_team(ctx, my_roster_id, username)
    res = await asyncio.to_thread(sim.run, simulations)

    def pct(x: float) -> str:
        return "<1%" if 0 < x < 0.005 else ">99%" if 0.995 < x < 1 else f"{round(100 * x)}%"

    rows = []
    for t in sorted(ctx.teams.values(), key=lambda t: (-res.playoffs[t.roster_id], -res.champion[t.roster_id])):
        rid = t.roster_id
        rows.append(
            {
                "team": ctx.team_label(rid),
                "roster_id": rid,
                "record": t.record,
                "proj_final_wins": r1(res.wins[rid]),
                "playoffs": pct(res.playoffs[rid]),
                "bye": pct(res.bye[rid]),
                "top_seed": pct(res.top_seed[rid]),
                "champion": pct(res.champion[rid]),
                "avg_finish": r1(res.seed[rid]),
            }
        )
    result: dict[str, Any] = {
        "league": ctx.name,
        "simulations": simulations,
        "regular_season_weeks_left": sim.reg_weeks,
        "playoff_teams": sim.n_playoff,
        "playoff_weeks": ctx.playoff_weeks,
        "teams": rows,
    }
    if me is not None:
        sched = []
        for w in sim.reg_weeks:
            opp = next((b if a == me.roster_id else a for a, b in sim.schedule[w] if me.roster_id in (a, b)), None)
            if opp is None:
                continue
            m_mean, m_var = team_projection(val, me.players, w)
            o_mean, o_var = team_projection(val, ctx.team(opp).players, w)
            sched.append(
                {
                    "week": w,
                    "opponent": ctx.team_label(opp),
                    "opponent_record": ctx.team(opp).record,
                    "my_proj": r1(m_mean),
                    "opp_proj": r1(o_mean),
                    "win_prob": pct(win_probability(m_mean, m_var, o_mean, o_var)),
                }
            )
        result["my_team"] = me.name
        result["my_remaining_schedule"] = sched
    notes = [
        "Each team is assumed to start its best projected lineup every week.",
        f"This week's games are simulated from projections; points already scored in week {ctx.current_week} aren't counted."
        if ctx.current_week
        else None,
        "Rosters are frozen at today's; future pickups and trades aren't modeled.",
    ]
    if ctx.settings.get("divisions"):
        notes.append("Divisions aren't modeled: seeding uses overall wins, then points for.")
    if sim.median_game:
        notes.append("Includes the league's extra weekly game against the median score.")
    result["notes"] = [n for n in notes if n] + _notes(prod)
    return result


async def _season_sim(ctx: LeagueContext, val: Valuation) -> SeasonSim:
    start = int(ctx.settings.get("playoff_week_start") or 0)
    weeks = [w for w in val.weeks if w is not None and (not start or w < start)]
    responses = await asyncio.gather(*(api.matchups(ctx.league_id, w) for w in weeks))
    return SeasonSim(ctx, val, {w: pairings(r) for w, r in zip(weeks, responses)})


def _odds_change(before: SimResult, after: SimResult, rid: int) -> dict[str, str]:
    def fmt(b: float, a: float) -> str:
        d = round(100 * (a - b))
        return f"{round(100 * b)}% -> {round(100 * a)}% ({'+' if d >= 0 else ''}{d})"

    return {
        "playoffs": fmt(before.playoffs[rid], after.playoffs[rid]),
        "bye": fmt(before.bye[rid], after.bye[rid]),
        "champion": fmt(before.champion[rid], after.champion[rid]),
    }


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
