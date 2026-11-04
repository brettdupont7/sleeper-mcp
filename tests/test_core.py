import asyncio
import time

from sleeper_mcp.http import RateLimiter, TTLCache
from sleeper_mcp.players import PlayerDB, _slim
from sleeper_mcp.scoring import describe, points
from sleeper_mcp.sleeper import best_lineup, position_targets
from sleeper_mcp.undocumented import Schedule

LINEUP = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF"]


def test_points_matches_sleeper_example():
    # Joe Burrow, 2026 week 8: Sleeper scored this 42.48 in a 4pt-pass-TD league.
    stats = {"pass_yd": 462, "pass_td": 5, "pass_int": 3, "rush_yd": 10, "rush_td": 1, "gp": 1}
    scoring = {"pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "rush_yd": 0.1, "rush_td": 6}
    assert points(stats, scoring) == 42.48
    assert points(None, scoring) is None


def test_describe_formats():
    assert describe({"rec": 1.0})["format"] == "PPR"
    assert describe({"rec": 0.5})["format"] == "Half-PPR"
    assert describe({"rec": 1.0, "bonus_rec_te": 0.5})["premiums"] == {"bonus_rec_te": 0.5}


def test_position_targets_standard_lineup():
    t = position_targets(LINEUP)
    assert t == {"QB": 1, "RB": 4, "WR": 4, "TE": 1, "K": 1, "DEF": 1}


def test_position_targets_superflex():
    assert position_targets(["QB", "SUPER_FLEX", "RB", "WR"])["QB"] == 2


def test_schedule_byes_and_opponents():
    games = [
        {"week": 1, "home": "CIN", "away": "PIT"},
        {"week": 2, "home": "PIT", "away": "BAL"},
        {"week": 1, "home": "BAL", "away": "KC"},
        {"week": 2, "home": "KC", "away": "CIN"},
    ]
    s = Schedule(games)
    assert s.opponent("PIT", 1) == "@ CIN"
    assert s.opponent("PIT", 2) == "vs BAL"
    assert s.bye_week("PIT") is None
    s2 = Schedule(games + [{"week": 3, "home": "CIN", "away": "KC"}])
    assert s2.bye_week("PIT") == 3
    assert s2.opponent("PIT", 3) == "BYE"
    assert Schedule([]).opponent("PIT", 1) is None


def test_ttl_cache_expires():
    c = TTLCache()
    c.set("a", 1, ttl=0.05)
    assert c.get("a") == (True, 1)
    time.sleep(0.06)
    assert c.get("a") == (False, None)


def test_rate_limiter_blocks_past_limit():
    async def run():
        rl = RateLimiter(3, period=0.2)
        t = time.monotonic()
        for _ in range(4):
            await rl.acquire()
        return time.monotonic() - t

    assert asyncio.run(run()) >= 0.19


def _db(raw):
    db = PlayerDB.__new__(PlayerDB)
    db.players = _slim(raw)
    db._index()
    return db


def test_player_resolution():
    db = _db({
        "1": {"full_name": "Ja'Marr Chase", "position": "WR", "team": "CIN", "fantasy_positions": ["WR"], "search_rank": 3},
        "2": {"full_name": "Kenneth Walker III", "position": "RB", "team": "SEA", "fantasy_positions": ["RB"]},
        "3": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "search_rank": 1},
        "4": {"full_name": "Josh Allen", "position": "LB", "team": "JAX", "search_rank": 500},
        "PIT": {"first_name": "Pittsburgh", "last_name": "Steelers", "position": "DEF", "team": "PIT"},
    })
    assert db.resolve("jamarr chase") == ["1"]
    assert db.resolve("Kenneth Walker") == ["2"]
    assert db.resolve("Josh Allen") == ["3", "4"]
    assert db.resolve("Josh Allen", within=["4"]) == ["4"]
    assert db.resolve("pit") == ["PIT"]
    assert db.name("PIT") == "Pittsburgh Steelers"


def test_best_lineup_fills_flex_with_leftovers():
    db = _db({
        "q": {"full_name": "Q", "position": "QB", "fantasy_positions": ["QB"]},
        "r1": {"full_name": "R1", "position": "RB", "fantasy_positions": ["RB"]},
        "r2": {"full_name": "R2", "position": "RB", "fantasy_positions": ["RB"]},
        "r3": {"full_name": "R3", "position": "RB", "fantasy_positions": ["RB"]},
        "w1": {"full_name": "W1", "position": "WR", "fantasy_positions": ["WR"]},
    })
    vals = {"q": 20, "r1": 15, "r2": 12, "r3": 9, "w1": 10}
    total, filled = best_lineup(["QB", "RB", "RB", "WR", "FLEX"], list(vals), db, vals.get)
    assert total == 66
    assert filled["FLEX"] == [("r3", 9)]
