"""Trade math: replacement level, week-by-week lineups, and trade search.

Everything here measures how a roster's *starting lineup* changes, because
that's what wins fantasy weeks. Two ideas make that honest:

- Replacement level. A team can always pick up a free agent, so a lineup slot
  is never truly worth zero. Each position's replacement level is the average
  projection of the top few free agents in this league. A player's value over
  replacement (VOR) is what he's actually worth to a team.
- Streaming. When a roster can't fill a slot in some week (byes, injuries), the
  lineup simulation starts a replacement-level free agent there instead of a
  zero. That way a backup QB is worth exactly what he adds over the waiver wire
  in the weeks he'd actually start.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Iterable

from .sleeper import (
    SLOT_ELIGIBILITY,
    SLOT_ORDER,
    LeagueContext,
    Production,
    Team,
    is_unavailable,
    nfl_team_ok,
    r1,
)

REPLACEMENT_DEPTH = 3  # average of the top N free agents at a position
UNTRADED_POSITIONS = {"K", "DEF"}  # almost never worth a trade slot
DEFAULT_MAX_PARTNER_LOSS = 2.0  # lineup points per week the partner may lose by default


def free_agents(ctx: LeagueContext, prod: Production, positions: set[str] | None = None) -> list[str]:
    """Unrostered, active players on an NFL team at a starting position."""
    wanted = positions or ctx.relevant_positions
    rostered = ctx.rostered
    db = prod.db
    pool = set(prod.season_stats)
    for wk in prod.week_proj.values():
        pool |= set(wk)
    out = []
    for pid in pool:
        if pid in rostered:
            continue
        p = db.get(pid)
        if not p or not nfl_team_ok(p.get("team")):
            continue
        if p.get("position") != "DEF" and p.get("active") is False:
            continue
        if set(db.positions(pid)) & wanted:
            out.append(pid)
    return out


def _spread(results: list[dict], forced: tuple[str, ...] = (), per_offer: int = 2) -> list[dict]:
    """Stop one offer (e.g. a backup QB) from filling the whole list.

    Each set of players sent appears at most `per_offer` times. Players the user
    forced into every trade don't count: if the only thing sent is the forced
    player, there's no cap, since varying what comes back is the point.
    """
    seen: dict[frozenset[str], int] = {}
    out = []
    for d in results:
        key = frozenset(d["give"]) - set(forced)
        if key:
            if seen.get(key, 0) >= per_offer:
                continue
            seen[key] = seen.get(key, 0) + 1
        out.append(d)
    return out


def _top_mean(values: Iterable[float], n: int) -> float:
    top = sorted(values, reverse=True)[:n]
    return sum(top) / len(top) if top else 0.0


@dataclass
class LineupResult:
    total: float
    starters: list[tuple[str, str | None, float]]  # (slot, pid or None if streamed, value)
    holes: list[str]  # slots with no healthy, non-bye player at all


def lineup(
    slots: list[str],
    pids: Iterable[str],
    positions: Callable[[str], list[str]],
    value: Callable[[str], float],
    replacement: dict[str, float],
) -> LineupResult:
    """Greedy best lineup, streaming a replacement-level free agent when that beats the bench."""
    pool = sorted(pids, key=value, reverse=True)
    used: set[str] = set()
    ordered = sorted(slots, key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else len(SLOT_ORDER))
    starters: list[tuple[str, str | None, float]] = []
    holes: list[str] = []
    total = 0.0
    for slot in ordered:
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        pick = next((p for p in pool if p not in used and set(positions(p)) & elig), None)
        pick_val = value(pick) if pick else 0.0
        if pick_val <= 0:
            holes.append(slot)
        stream_val = max((replacement.get(pos, 0.0) for pos in elig), default=0.0)
        if pick is not None and pick_val >= stream_val:
            used.add(pick)
            starters.append((slot, pick, pick_val))
            total += pick_val
        else:
            starters.append((slot, None, stream_val))
            total += stream_val
    return LineupResult(total, starters, holes)


class Valuation:
    """Replacement levels and lineup simulation for one league."""

    def __init__(self, ctx: LeagueContext, prod: Production):
        self.ctx = ctx
        self.prod = prod
        self.db = prod.db
        self.slots = ctx.starting_slots
        remaining = [w for w in ctx.remaining_weeks if w in prod.week_proj]
        self.weeks: list[int | None] = remaining or [None]
        self.playoff_weeks = [w for w in ctx.playoff_weeks if w in remaining]
        self.fa = free_agents(ctx, prod)

        by_pos: dict[str, list[str]] = {}
        for pid in self.fa:
            by_pos.setdefault(self.db.get(pid).get("position") or "?", []).append(pid)
        self.replacement_ros = {
            pos: _top_mean((prod.ros_value(p) for p in pids if not is_unavailable(self.db.get(p))), REPLACEMENT_DEPTH)
            for pos, pids in by_pos.items()
        }
        self.replacement_week = {
            w: {pos: _top_mean((prod.week_value(p, w) for p in pids), REPLACEMENT_DEPTH) for pos, pids in by_pos.items()}
            for w in self.weeks
        }

    # ---- single-player value ---------------------------------------------------

    def vor(self, pid: str) -> float:
        """Projected points per game over replacement at the player's position."""
        pos = self.db.get(pid).get("position") or "?"
        return self.prod.ros_value(pid) - self.replacement_ros.get(pos, 0.0)

    # ---- lineups ------------------------------------------------------------------

    def typical_week(self, pids: Iterable[str]) -> float:
        """Lineup points in an average rest-of-season week (fast; used for screening)."""
        return lineup(self.slots, pids, self.db.positions, self.prod.ros_value, self.replacement_ros).total

    def week(self, pids: Iterable[str], w: int | None) -> LineupResult:
        return lineup(
            self.slots, pids, self.db.positions, lambda p: self.prod.week_value(p, w), self.replacement_week[w]
        )

    def season(self, pids: list[str]) -> dict[int | None, LineupResult]:
        return {w: self.week(pids, w) for w in self.weeks}

    def compare(self, before: list[str], after: list[str]) -> dict:
        """Week-by-week lineup change for one roster, plus totals."""
        b, a = self.season(before), self.season(after)
        per_week: dict[str, float | None] = {}
        diff_total = diff_playoffs = 0.0
        for w in self.weeks:
            d = a[w].total - b[w].total
            label = "typical week" if w is None else f"wk{w}" + (" (playoffs)" if w in self.playoff_weeks else "")
            per_week[label] = r1(d)
            diff_total += d
            if w in self.playoff_weeks:
                diff_playoffs += d
        out: dict = {
            "per_week": per_week,
            "rest_of_season_total": r1(diff_total),
            "avg_per_week": r1(diff_total / len(self.weeks)),
        }
        if self.playoff_weeks:
            out["playoff_weeks_total"] = r1(diff_playoffs)
            out["regular_season_total"] = r1(diff_total - diff_playoffs)
        holes_b = {w: b[w].holes for w in self.weeks if b[w].holes}
        holes_a = {w: a[w].holes for w in self.weeks if a[w].holes}
        if holes_b != holes_a:

            def fmt(d: dict) -> dict | None:
                return {("typical" if w is None else f"wk{w}"): s for w, s in d.items()} or None

            # Slots with no healthy player who has a game: you'd have to pick someone up.
            out["lineup_holes"] = {"before": fmt(holes_b), "after": fmt(holes_a)}
        return out

    # ---- trade search -------------------------------------------------------------

    def tradeable(self, team: Team) -> list[str]:
        """Players worth putting in a trade: above replacement, not K/DEF, not on IR."""
        out = []
        for pid in team.players:
            p = self.db.get(pid)
            if (p.get("position") or "") in UNTRADED_POSITIONS or pid in team.reserve or pid in team.taxi:
                continue
            if self.vor(pid) > 0.5:
                out.append(pid)
        return out

    def search(
        self,
        me: Team,
        partners: list[Team],
        *,
        max_per_side: int = 2,
        receive_positions: set[str] | None = None,
        min_partner_gain: float = 0.25,
        max_partner_loss: float | None = DEFAULT_MAX_PARTNER_LOSS,
        exclude: set[str] | None = None,
        must_send: list[str] | None = None,
        shortlist: int = 40,
    ) -> tuple[list[dict], list[dict]]:
        """Find trades that help you. Screens on a typical week, then re-scores the
        best candidates week by week through the playoffs.

        Returns (win_win, one_sided): trades where both lineups improve, and
        trades that help you while costing the partner up to `max_partner_loss`
        lineup points per week (None = no limit). With the default limit,
        one-sided trades are ranked closest-to-fair first; with a looser limit,
        by how much they help you.

        `exclude` players never appear on either side (your untouchables, or
        players you don't want). Every result sends all of `must_send`, even
        players the normal filter would skip (K/DEF, IR, below replacement).
        """
        exclude = exclude or set()
        forced = tuple(must_send or ())
        if len(forced) > max_per_side:
            raise ValueError(f"must_send has {len(forced)} players but max_per_side is {max_per_side}.")
        floor = float("-inf") if max_partner_loss is None else -abs(max_partner_loss)
        favor_me = max_partner_loss is None or abs(max_partner_loss) > DEFAULT_MAX_PARTNER_LOSS
        my_base = self.typical_week(me.players)
        mine = [p for p in self.tradeable(me) if p not in exclude and p not in forced]
        screened: list[tuple[float, Team, tuple[str, ...], tuple[str, ...], float, float]] = []

        for t in partners:
            theirs = [p for p in self.tradeable(t) if p not in exclude]
            if receive_positions:
                theirs_recv = [p for p in theirs if set(self.db.positions(p)) & receive_positions]
            else:
                theirs_recv = theirs
            if not theirs_recv:
                continue
            their_base = self.typical_week(t.players)
            for n_out in range(1, max_per_side + 1):
                for n_in in range(1, max_per_side + 1):
                    if n_out + n_in > 3:  # 2-for-2 rarely gets accepted and explodes the search
                        continue
                    if n_out < len(forced):
                        continue
                    gives = (forced + extra for extra in itertools.combinations(mine, n_out - len(forced)))
                    for give in gives:
                        for get in itertools.combinations(theirs, n_in):
                            if receive_positions and not any(p in theirs_recv for p in get):
                                continue
                            my_after = [p for p in me.players if p not in give] + list(get)
                            their_after = [p for p in t.players if p not in get] + list(give)
                            my_gain = self.typical_week(my_after) - my_base
                            if my_gain <= 0.3:
                                continue
                            their_gain = self.typical_week(their_after) - their_base
                            if their_gain < floor:
                                continue
                            screened.append((t, give, get, my_gain, their_gain))

        def pick(rows: list, key) -> list:
            # Keep variety: at most 3 candidates per partner into the detailed pass.
            # Also skip repeats of the same return package with a different offer:
            # the first (best-ranked) way to get those players is the one worth showing.
            per_partner: dict[int, int] = {}
            seen: set[tuple[int, frozenset[str]]] = set()
            out = []
            for row in sorted(rows, key=key, reverse=True):
                rid = row[0].roster_id
                package = (rid, frozenset(row[2]))
                if per_partner.get(rid, 0) >= 3 or package in seen:
                    continue
                seen.add(package)
                per_partner[rid] = per_partner.get(rid, 0) + 1
                out.append(row)
                if len(out) >= shortlist:
                    break
            return out

        mutual = pick([r for r in screened if r[4] >= min_partner_gain], key=lambda r: r[3] + 0.5 * r[4])
        # One-sided: must clearly help you. Default: closest to fair first.
        # Looser limit: the user wants the biggest upgrades, so rank by your gain.
        one_sided_key = (lambda r: r[3]) if favor_me else (lambda r: r[3] + r[4])
        near = pick([r for r in screened if r[4] < min_partner_gain and r[3] >= 1.0], key=one_sided_key)
        win_win = [d for d in (self._detail(me, *r[:3]) for r in mutual) if d["their_total"] > 0 and d["my_total"] > 0]
        weeks = len(self.weeks)
        one_sided = [
            d
            for d in (self._detail(me, *r[:3]) for r in near)
            if d["my_total"] > 0 and d["their_total"] <= 0 and d["their_total"] / weeks >= floor
        ]
        win_win.sort(key=lambda d: d["my_total"], reverse=True)
        if favor_me:
            one_sided.sort(key=lambda d: d["my_total"], reverse=True)
        else:
            one_sided.sort(key=lambda d: d["my_total"] + d["their_total"], reverse=True)
        return _spread(win_win, forced), _spread(one_sided, forced)

    def _detail(self, me: Team, t: Team, give: tuple[str, ...], get: tuple[str, ...]) -> dict:
        my_after = [p for p in me.players if p not in give] + list(get)
        their_after = [p for p in t.players if p not in get] + list(give)
        mine_cmp = self.compare(me.players, my_after)
        theirs_cmp = self.compare(t.players, their_after)
        return {
            "partner": t,
            "give": list(give),
            "get": list(get),
            "mine": mine_cmp,
            "theirs": theirs_cmp,
            "my_total": mine_cmp["rest_of_season_total"] or 0.0,
            "their_total": theirs_cmp["rest_of_season_total"] or 0.0,
            "vor_sent": sum(self.vor(p) for p in give),
            "vor_received": sum(self.vor(p) for p in get),
        }
