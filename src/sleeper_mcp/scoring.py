"""Fantasy scoring under a specific league's `scoring_settings`.

Sleeper's scoring keys use the same names as its stat keys (pass_yd, rec,
bonus_rec_te, pts_allow_7_13, ...), so league points are a dot product.
"""

from __future__ import annotations

from typing import Any


def points(stats: dict[str, float] | None, scoring: dict[str, float]) -> float | None:
    if not stats:
        return None
    total = 0.0
    for key, weight in scoring.items():
        val = stats.get(key)
        if val:
            total += float(val) * float(weight)
    return round(total, 2)


def describe(scoring: dict[str, float]) -> dict[str, Any]:
    """Short human summary of the scoring rules that matter for player value."""
    rec = scoring.get("rec", 0)
    fmt = {1.0: "PPR", 0.5: "Half-PPR", 0.0: "Standard"}.get(float(rec), f"{rec} per reception")
    out: dict[str, Any] = {
        "format": fmt,
        "pass_td": scoring.get("pass_td", 4),
        "pass_yd_per_pt": round(1 / scoring["pass_yd"], 1) if scoring.get("pass_yd") else None,
        "pass_int": scoring.get("pass_int", 0),
        "rush_rec_yd_per_pt": round(1 / scoring["rush_yd"], 1) if scoring.get("rush_yd") else None,
    }
    premiums = {
        k: v
        for k, v in scoring.items()
        if v and (k.startswith("bonus_rec_") or k in ("bonus_fd_rb", "bonus_fd_wr", "bonus_fd_te", "bonus_fd_qb"))
    }
    if premiums:
        out["premiums"] = premiums  # e.g. bonus_rec_te = TE premium
    big = {k: v for k, v in scoring.items() if v and k.startswith(("bonus_pass_yd", "bonus_rush_yd", "bonus_rec_yd"))}
    if big:
        out["yardage_bonuses"] = big
    if scoring.get("pass_cmp") or scoring.get("pass_inc"):
        out["pass_cmp"] = scoring.get("pass_cmp", 0)
        out["pass_inc"] = scoring.get("pass_inc", 0)
    if scoring.get("rush_att"):
        out["rush_att"] = scoring["rush_att"]
    idp = sorted(k for k, v in scoring.items() if v and k.startswith("idp_"))
    if idp:
        out["idp_scoring"] = True
    return out
