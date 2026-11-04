from sleeper_mcp.season import bracket_order, pairings, score_variance, win_probability


def test_bracket_order_keeps_top_seeds_apart():
    assert bracket_order(4) == [1, 4, 2, 3]
    order = bracket_order(8)
    assert order == [1, 8, 4, 5, 2, 7, 3, 6]
    # 1 and 2 are in opposite halves, so they can only meet in the final.
    assert order.index(1) < 4 <= order.index(2)


def test_six_team_bracket_gives_top_two_byes():
    # With 6 playoff teams in an 8-slot bracket, seeds 7 and 8 don't exist,
    # so 1 and 2 face an empty slot in round one.
    order = bracket_order(8)
    first_round = [(order[i], order[i + 1]) for i in range(0, 8, 2)]
    byes = [a for a, b in first_round if b > 6]
    assert sorted(byes) == [1, 2]


def test_pairings_groups_by_matchup_id():
    rows = [
        {"roster_id": 1, "matchup_id": 3},
        {"roster_id": 2, "matchup_id": 1},
        {"roster_id": 3, "matchup_id": 3},
        {"roster_id": 4, "matchup_id": 1},
        {"roster_id": 5, "matchup_id": None},
    ]
    assert sorted(pairings(rows)) == [(1, 3), (2, 4)]


def test_win_probability_is_symmetric_and_sane():
    assert abs(win_probability(120, 400, 120, 400) - 0.5) < 1e-9
    p = win_probability(130, 400, 120, 400)
    assert 0.6 < p < 0.7
    assert abs(p + win_probability(120, 400, 130, 400) - 1) < 1e-9


def test_score_variance_has_a_floor_and_ignores_zeros():
    assert score_variance([0.0, 0.0]) == 0
    assert score_variance([2.0]) == 9.0  # floor of 3 points
    assert abs(score_variance([20.0]) - (0.55 * 20) ** 2) < 1e-9
