from sleeper_mcp.sleeper import LeagueContext
from sleeper_mcp.trade import lineup

POS = {"q1": ["QB"], "q2": ["QB"], "r1": ["RB"], "r2": ["RB"], "w1": ["WR"]}


def positions(p):
    return POS[p]


def test_lineup_streams_replacement_into_empty_slot():
    vals = {"q1": 20, "r1": 15, "w1": 10}
    res = lineup(["QB", "RB", "RB", "WR"], vals, positions, vals.get, {"RB": 6, "WR": 8, "QB": 14})
    assert res.total == 20 + 15 + 6 + 10
    assert ("RB", None, 6) in res.starters
    assert res.holes == ["RB"]


def test_backup_qb_only_counts_over_replacement_on_bye():
    repl = {"QB": 14}
    starter_on_bye = {"q1": 0, "q2": 17}
    with_backup = lineup(["QB"], ["q1", "q2"], positions, starter_on_bye.get, repl)
    without = lineup(["QB"], ["q1"], positions, starter_on_bye.get, repl)
    assert with_backup.total - without.total == 3  # 17 - 14, not 17
    assert without.holes == ["QB"] and with_backup.holes == []


def test_free_agent_better_than_starter_is_not_a_hole():
    vals = {"q1": 10}
    res = lineup(["QB"], ["q1"], positions, vals.get, {"QB": 14})
    assert res.total == 14 and res.holes == []


def _ctx(settings, state=None):
    league = {"league_id": "1", "season": "2026", "settings": settings, "roster_positions": []}
    return LeagueContext(league, [], [], state or {"season": "2026", "league_season": "2026", "season_type": "regular", "week": 9})


def test_playoff_weeks_and_remaining():
    ctx = _ctx({"playoff_week_start": 15, "playoff_teams": 6})
    assert ctx.playoff_weeks == [15, 16, 17]
    assert ctx.remaining_weeks == list(range(9, 18))
    assert _ctx({"playoff_week_start": 15, "playoff_teams": 4, "playoff_round_type": 2}).playoff_weeks == [15, 16, 17, 18]
    assert _ctx({"playoff_week_start": 15, "playoff_teams": 4, "playoff_round_type": 1}).playoff_weeks == [15, 16, 17]
    assert _ctx({"playoff_week_start": 0}).playoff_weeks == []


class _FakeDB:
    def __init__(self, pos):
        self.pos = pos

    def positions(self, p):
        return [self.pos[p]]

    def get(self, p):
        return {"position": self.pos[p]}


def _valuation(ros: dict[str, float], pos: dict[str, str], slots: list[str]):
    """A Valuation with fixed per-game values and no free agents worth starting."""
    from sleeper_mcp.trade import Valuation

    v = Valuation.__new__(Valuation)
    v.db = _FakeDB(pos)
    v.slots = slots
    v.weeks = [None]
    v.playoff_weeks = []
    v.replacement_ros = {}
    v.replacement_week = {None: {}}

    class Prod:
        ros_value = staticmethod(lambda p: ros[p])
        week_value = staticmethod(lambda p, w: ros[p])

    v.prod = Prod()
    return v


def _team(rid, players):
    from sleeper_mcp.sleeper import Team

    return Team(rid, f"u{rid}", f"T{rid}", f"o{rid}", 0, 0, 0, 0.0, 0.0, None, players, [], [], [], None, None)


def test_max_partner_loss_controls_one_sided_trades():
    # I have two RBs and a weak WR; they have a stud WR and weak RBs.
    pos = {"a_rb1": "RB", "a_rb2": "RB", "a_wr": "WR", "b_wr1": "WR", "b_wr2": "WR", "b_rb": "RB"}
    ros = {"a_rb1": 15, "a_rb2": 12, "a_wr": 3, "b_wr1": 20, "b_wr2": 4, "b_rb": 5}
    v = _valuation(ros, pos, ["RB", "WR"])
    me, them = _team(1, ["a_rb1", "a_rb2", "a_wr"]), _team(2, ["b_wr1", "b_wr2", "b_rb"])

    # a_rb2 for b_wr1: I gain 17 (3 -> 20 at WR), they lose 9 (20 -> 4 WR, 5 -> 12 RB = +7 -16).
    win, strict = v.search(me, [them], max_per_side=1, max_partner_loss=2)
    assert not any(d["get"] == ["b_wr1"] for d in strict)
    win, loose = v.search(me, [them], max_per_side=1, max_partner_loss=None)
    assert loose and loose[0]["get"] == ["b_wr1"]
    assert loose[0]["my_total"] == 17 and loose[0]["their_total"] == -9


def _three_team_setup():
    pos = {"a_rb1": "RB", "a_rb2": "RB", "a_rb3": "RB", "a_wr": "WR", "b_wr1": "WR", "b_wr2": "WR", "b_rb": "RB"}
    ros = {"a_rb1": 15, "a_rb2": 12, "a_rb3": 11, "a_wr": 3, "b_wr1": 20, "b_wr2": 4, "b_rb": 5}
    v = _valuation(ros, pos, ["RB", "WR"])
    return v, _team(1, ["a_rb1", "a_rb2", "a_rb3", "a_wr"]), _team(2, ["b_wr1", "b_wr2", "b_rb"])


def test_exclude_players_removes_them_from_both_sides():
    v, me, them = _three_team_setup()
    win, one = v.search(me, [them], max_per_side=1, max_partner_loss=None, exclude={"a_rb2", "b_wr1"})
    for d in win + one:
        assert "a_rb2" not in d["give"] and "b_wr1" not in d["get"]


def test_must_send_is_in_every_result():
    v, me, them = _three_team_setup()
    win, one = v.search(me, [them], max_per_side=2, max_partner_loss=None, must_send=["a_rb1"])
    results = win + one
    assert results and all("a_rb1" in d["give"] for d in results)


def test_must_send_larger_than_side_limit_is_rejected():
    import pytest

    v, me, them = _three_team_setup()
    with pytest.raises(ValueError):
        v.search(me, [them], max_per_side=1, must_send=["a_rb1", "a_rb2"])


def test_one_offer_cannot_fill_the_list():
    from sleeper_mcp.trade import _spread

    rows = [{"give": ["cousins"], "get": [f"x{i}"]} for i in range(5)] + [{"give": ["egbuka"], "get": ["y"]}]
    assert [d["give"] for d in _spread(rows)] == [["cousins"], ["cousins"], ["egbuka"]]
    # A forced player alone is never capped: varying what comes back is the point.
    assert len(_spread(rows, forced=("cousins",))) == 6
