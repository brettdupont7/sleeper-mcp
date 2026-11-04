"""Weekly lineup checks and season simulation (playoff odds).

Both build on trade.Valuation: each team's projected score in a week is its best
lineup under the league's scoring, with byes and Out/IR as zeros and empty slots
streamed at replacement level.

Weekly scores are random around that projection. Each starter's standard
deviation is PLAYER_CV of his projection (at least PLAYER_SD_FLOOR), treated as
independent. That gives typical team swings of about 20 points, in line with
real fantasy weeks.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

from .sleeper import SLOT_ELIGIBILITY, LeagueContext, Production, Team, injury_text, is_unavailable, r1
from .trade import Valuation, lineup

PLAYER_CV = 0.55
PLAYER_SD_FLOOR = 3.0
DEFAULT_SIMS = 5000


def score_variance(values: list[float]) -> float:
    return sum(max(PLAYER_SD_FLOOR, PLAYER_CV * v) ** 2 for v in values if v > 0)


def win_probability(mean_a: float, var_a: float, mean_b: float, var_b: float) -> float:
    sd = math.sqrt(var_a + var_b) or 1.0
    return 0.5 * (1 + math.erf((mean_a - mean_b) / (sd * math.sqrt(2))))


def team_projection(val: Valuation, pids: list[str], week: int | None) -> tuple[float, float]:
    """Projected score and variance. The current week uses only the roster as it is
    (pickups may not clear in time); later weeks assume teams stream free agents
    into empty or weak slots."""
    res = val.week(pids, week, stream=week is None or week != val.ctx.current_week)
    return res.total, score_variance([v for _, _, v in res.starters])


def pairings(matchups: list[dict]) -> list[tuple[int, int]]:
    """[(roster_a, roster_b)] from a week's /matchups response."""
    by_id: dict[Any, list[int]] = {}
    for m in matchups:
        if m.get("matchup_id") is not None:
            by_id.setdefault(m["matchup_id"], []).append(int(m["roster_id"]))
    return [(a[0], a[1]) for a in by_id.values() if len(a) == 2]


# ---- weekly lineup check ----------------------------------------------------------


def lineup_check(
    ctx: LeagueContext,
    prod: Production,
    val: Valuation,
    team: Team,
    week: int,
    opponent: Team | None,
) -> dict[str, Any]:
    db = prod.db
    value = lambda p: prod.week_value(p, week)  # noqa: E731
    sched = prod.schedule

    def locked(pid: str | None) -> bool:
        return bool(pid) and sched.game_started(db.get(pid).get("team"), week)

    def flags(pid: str) -> list[str]:
        p = db.get(pid)
        out = []
        opp = sched.opponent(p.get("team"), week)
        if opp == "BYE":
            out.append("on bye")
        inj = injury_text(p)
        if inj:
            out.append(inj)
        if locked(pid):
            out.append("game started (locked)")
        return out

    def row(slot: str, pid: str | None) -> dict[str, Any]:
        if not pid:
            return {"slot": slot, "player": "(empty)", "proj": 0.0, "flags": ["empty slot"]}
        p = db.get(pid)
        d: dict[str, Any] = {
            "slot": slot,
            "player": f"{p.get('full_name', pid)} ({p.get('position')}, {p.get('team') or 'FA'})",
            "proj": r1(value(pid)),
        }
        opp = sched.opponent(p.get("team"), week)
        if opp and opp != "BYE":
            d["opp"] = opp
        f = flags(pid)
        if f:
            d["flags"] = f
        return d

    current = team.lineup_slots or [(s, None) for s in ctx.starting_slots]
    current_ids = {pid for _, pid in current if pid}
    current_total = sum(value(pid) for _, pid in current if pid)

    # Best legal lineup: locked starters stay put, locked bench players can't come in.
    fixed = [(slot, pid) for slot, pid in current if locked(pid)]
    open_slots = [slot for slot, pid in current if not locked(pid)]
    pool = [
        p
        for p in team.players
        if p not in team.reserve and p not in team.taxi and not locked(p)
    ]
    best = lineup(open_slots, pool, db.positions, value, {})
    best_rows = [(slot, pid) for slot, pid in fixed] + [(slot, pid) for slot, pid, _ in best.starters]
    best_ids = {pid for _, pid in best_rows if pid}
    best_total = sum(value(pid) for _, pid in best_rows if pid)

    start = sorted(best_ids - current_ids, key=value, reverse=True)
    sit = sorted(current_ids - best_ids, key=value, reverse=True)
    changes = []
    for i in range(max(len(start), len(sit))):
        s_in = start[i] if i < len(start) else None
        s_out = sit[i] if i < len(sit) else None
        if s_in and s_out:
            changes.append(
                f"Start {db.name(s_in)} ({r1(value(s_in))}) over {db.name(s_out)} ({r1(value(s_out))})"
                + (f": {', '.join(flags(s_out))}" if flags(s_out) else "")
            )
        elif s_in:
            changes.append(f"Start {db.name(s_in)} ({r1(value(s_in))}) in an empty slot")
        elif s_out:
            changes.append(f"Bench {db.name(s_out)} ({r1(value(s_out))})")

    # Holes and big free-agent upgrades for this week.
    fa_by_pos: dict[str, list[str]] = {}
    for pid in val.fa:
        p = db.get(pid)
        if is_unavailable(p) or value(pid) <= 0:
            continue
        fa_by_pos.setdefault(p.get("position") or "?", []).append(pid)
    for pos in fa_by_pos:
        fa_by_pos[pos].sort(key=value, reverse=True)

    def best_fas(slot: str, n: int = 3) -> list[str]:
        elig = SLOT_ELIGIBILITY.get(slot, {slot})
        cands = [p for pos in elig for p in fa_by_pos.get(pos, [])[:n]]
        return sorted(cands, key=value, reverse=True)[:n]

    pickups = []
    for slot, pid in best_rows:
        if locked(pid):
            continue
        mine = value(pid) if pid else 0.0
        fas = best_fas(slot)
        if not fas:
            continue
        gain = value(fas[0]) - mine
        if pid is None or mine <= 0 or gain >= 3.0:
            pickups.append(
                {
                    "slot": slot,
                    "current": db.name(pid) if pid else "(empty)",
                    "current_proj": r1(mine),
                    "free_agents": [f"{db.label(p)}: {r1(value(p))}" for p in fas],
                }
            )

    out: dict[str, Any] = {
        "week": week,
        "current_lineup": [row(slot, pid) for slot, pid in current],
        "current_projection": r1(current_total),
        "best_lineup_projection": r1(best_total),
        "changes": changes or "Your lineup is already the best projected one.",
    }
    problems = [
        f"{slot}: {db.name(pid) if pid else '(empty)'} - {', '.join(flags(pid)) if pid else 'empty slot'}"
        for slot, pid in current
        if not pid or (value(pid) <= 0 and not locked(pid))
    ]
    if problems:
        out["problems"] = problems
    if pickups:
        out["free_agent_options"] = pickups
    questionable = [
        f"{db.name(pid)}: {injury_text(db.get(pid))}"
        for _, pid in best_rows
        if pid and (db.get(pid).get("injury_status") or "") in ("Questionable", "Doubtful")
    ]
    if questionable:
        out["game_time_decisions"] = questionable

    if opponent is not None:
        my_var = score_variance([value(p) for _, p in best_rows if p])
        opp_mean, opp_var = team_projection(val, opponent.players, week)
        out["matchup"] = {
            "opponent": ctx.team_label(opponent.roster_id),
            "opponent_record": opponent.record,
            "my_projection": r1(best_total),
            "opponent_projection": r1(opp_mean),
            "my_win_probability": f"{round(100 * win_probability(best_total, my_var, opp_mean, opp_var))}%",
            "note": "Both sides assume their best projected lineup.",
        }
    return out


# ---- season simulation -------------------------------------------------------------


def bracket_order(size: int) -> list[int]:
    """Seed order for a standard bracket: 1 v size, ..., arranged so 1 and 2 meet last."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


@dataclass
class SimResult:
    playoffs: dict[int, float]
    bye: dict[int, float]
    top_seed: dict[int, float]
    champion: dict[int, float]
    wins: dict[int, float]
    seed: dict[int, float]


class SeasonSim:
    def __init__(
        self,
        ctx: LeagueContext,
        val: Valuation,
        schedule: dict[int, list[tuple[int, int]]],
    ):
        self.ctx = ctx
        self.val = val
        start = int(ctx.settings.get("playoff_week_start") or 0)
        self.reg_weeks = [w for w in val.weeks if w is not None and (not start or w < start) and schedule.get(w)]
        self.playoff_weeks = [w for w in val.weeks if w is not None and w in ctx.playoff_weeks]
        self.schedule = schedule
        self.n_playoff = int(ctx.settings.get("playoff_teams") or 0)
        self.median_game = bool(ctx.settings.get("league_average_match"))

    @property
    def possible(self) -> bool:
        return bool(self.reg_weeks) and self.n_playoff >= 2

    def _round_weeks(self, rounds: int) -> list[list[int | None]]:
        weeks = list(self.ctx.playoff_weeks)
        kind = self.ctx.settings.get("playoff_round_type") or 0
        if kind == 2:
            groups = [weeks[i : i + 2] for i in range(0, len(weeks), 2)]
        elif kind == 1:
            groups = [[w] for w in weeks[: rounds - 1]] + [weeks[rounds - 1 :]]
        else:
            groups = [[w] for w in weeks]
        groups = (groups + [[None]] * rounds)[:rounds]
        # Weeks without projections (shouldn't happen) fall back to a typical week.
        return [[w if w in self.val.replacement_week else None for w in g] or [None] for g in groups]

    def run(
        self, n: int = DEFAULT_SIMS, seed: int = 7, rosters: dict[int, list[str]] | None = None
    ) -> SimResult:
        ctx, val = self.ctx, self.val
        teams = list(ctx.teams.values())
        players = {t.roster_id: (rosters or {}).get(t.roster_id, t.players) for t in teams}
        size = 2 ** math.ceil(math.log2(self.n_playoff))
        rounds = int(math.log2(size))
        round_weeks = self._round_weeks(rounds)
        all_weeks = set(self.reg_weeks) | {w for g in round_weeks for w in g}
        proj = {
            (rid, w): team_projection(val, pids, w) for rid, pids in players.items() for w in all_weeks
        }
        sd = {k: math.sqrt(v) for k, (_, v) in proj.items()}
        mean = {k: m for k, (m, _) in proj.items()}
        order = bracket_order(size)
        rids = sorted(players)

        counts = {k: {rid: 0.0 for rid in rids} for k in ("playoffs", "bye", "top_seed", "champion", "wins", "seed")}
        rng = random.Random(seed)
        for _ in range(n):
            # Draw one z per team per week, in a fixed order, so runs with different
            # rosters share luck and their difference is the roster change.
            z = {(rid, w): rng.gauss(0, 1) for w in sorted(all_weeks, key=lambda x: x or 0) for rid in rids}
            score = lambda rid, w: mean[(rid, w)] + sd[(rid, w)] * z[(rid, w)]  # noqa: E731
            wins = {t.roster_id: t.wins + 0.5 * t.ties for t in teams}
            pf = {t.roster_id: t.pf for t in teams}
            for w in self.reg_weeks:
                week_scores = {rid: score(rid, w) for rid in rids}
                for a, b in self.schedule[w]:
                    if a in week_scores and b in week_scores:
                        wins[a if week_scores[a] > week_scores[b] else b] += 1
                for rid, sc in week_scores.items():
                    pf[rid] += sc
                if self.median_game:
                    med = sorted(week_scores.values())[len(week_scores) // 2]
                    for rid, sc in week_scores.items():
                        if sc > med:
                            wins[rid] += 1
            standings = sorted(rids, key=lambda r: (wins[r], pf[r]), reverse=True)
            seeds = {i + 1: rid for i, rid in enumerate(standings[: self.n_playoff])}
            for i, rid in enumerate(standings, 1):
                counts["seed"][rid] += i
                counts["wins"][rid] += wins[rid]
            byes = size - self.n_playoff
            for s, rid in seeds.items():
                counts["playoffs"][rid] += 1
                if s <= byes:
                    counts["bye"][rid] += 1
            counts["top_seed"][seeds[1]] += 1

            alive = [seeds.get(s) for s in order]  # None = empty slot (a bye for the opponent)
            for r in range(rounds):
                nxt = []
                for i in range(0, len(alive), 2):
                    a, b = alive[i], alive[i + 1]
                    if a is None or b is None:
                        nxt.append(a if b is None else b)
                        continue
                    sa = sum(score(a, w) if (a, w) in mean else 0 for w in round_weeks[r])
                    sb = sum(score(b, w) if (b, w) in mean else 0 for w in round_weeks[r])
                    nxt.append(a if sa > sb else b)
                alive = nxt
            if alive and alive[0] is not None:
                counts["champion"][alive[0]] += 1

        return SimResult(**{k: {rid: v / n for rid, v in d.items()} for k, d in counts.items()})
