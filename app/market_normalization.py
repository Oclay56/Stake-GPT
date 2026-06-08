from __future__ import annotations

from typing import Any

from .mlb_props import slug_key


SUPPORTED_MLB_PROP_MARKETS = {
    "hits",
    "singles",
    "total_bases",
    "home_runs",
    "rbi",
    "runs",
    "hits_runs_rbis",
    "batter_walks",
    "batter_strikeouts",
    "stolen_bases",
    "pitcher_strikeouts",
    "strikeouts",
    "outs_recorded",
    "hits_allowed",
    "earned_runs",
    "walks_allowed",
}


MLB_PROP_MARKET_ALIASES = {
    "hit": "hits",
    "hits": "hits",
    "player-hits": "hits",
    "player-hit": "hits",
    "single": "singles",
    "singles": "singles",
    "player-singles": "singles",
    "player-single": "singles",
    "one-base-hits": "singles",
    "one-baggers": "singles",
    "total-base": "total_bases",
    "total-bases": "total_bases",
    "bases": "total_bases",
    "player-total-base": "total_bases",
    "player-total-bases": "total_bases",
    "home-run": "home_runs",
    "home-runs": "home_runs",
    "homerun": "home_runs",
    "homeruns": "home_runs",
    "player-home-run": "home_runs",
    "player-home-runs": "home_runs",
    "rbi": "rbi",
    "rbis": "rbi",
    "runs-batted-in": "rbi",
    "player-rbi": "rbi",
    "player-rbis": "rbi",
    "run": "runs",
    "runs": "runs",
    "player-run": "runs",
    "player-runs": "runs",
    "hrr": "hits_runs_rbis",
    "h-r-r": "hits_runs_rbis",
    "hits-runs-rbi": "hits_runs_rbis",
    "hits-runs-rbis": "hits_runs_rbis",
    "hit-runs-rbis": "hits_runs_rbis",
    "hits-runs-and-rbis": "hits_runs_rbis",
    "hits-runs-rbis-hrr": "hits_runs_rbis",
    "walk": "batter_walks",
    "walks": "batter_walks",
    "bb": "batter_walks",
    "batter-walk": "batter_walks",
    "batter-walks": "batter_walks",
    "hitter-walk": "batter_walks",
    "hitter-walks": "batter_walks",
    "walks-drawn": "batter_walks",
    "base-on-balls": "batter_walks",
    "bases-on-balls": "batter_walks",
    "batter-k": "batter_strikeouts",
    "batter-ks": "batter_strikeouts",
    "batter-strikeout": "batter_strikeouts",
    "batter-strikeouts": "batter_strikeouts",
    "hitter-k": "batter_strikeouts",
    "hitter-ks": "batter_strikeouts",
    "hitter-strikeout": "batter_strikeouts",
    "hitter-strikeouts": "batter_strikeouts",
    "failed-attempt": "batter_strikeouts",
    "failed-attempts": "batter_strikeouts",
    "stolen-base": "stolen_bases",
    "stolen-bases": "stolen_bases",
    "player-stolen-base": "stolen_bases",
    "player-stolen-bases": "stolen_bases",
    "steal": "stolen_bases",
    "steals": "stolen_bases",
    "player-steal": "stolen_bases",
    "player-steals": "stolen_bases",
    "sb": "stolen_bases",
    "bases-stolen": "stolen_bases",
    "strikeout": "strikeouts",
    "strikeouts": "strikeouts",
    "k": "strikeouts",
    "ks": "strikeouts",
    "pitcher-strikeout": "pitcher_strikeouts",
    "pitcher-strikeouts": "pitcher_strikeouts",
    "pitching-strikeout": "pitcher_strikeouts",
    "pitching-strikeouts": "pitcher_strikeouts",
    "pitcher-k": "pitcher_strikeouts",
    "pitcher-ks": "pitcher_strikeouts",
    "outs-recorded": "outs_recorded",
    "pitcher-outs": "outs_recorded",
    "pitching-outs": "outs_recorded",
    "recorded-outs": "outs_recorded",
    "hits-allowed": "hits_allowed",
    "hit-allowed": "hits_allowed",
    "pitcher-hits-allowed": "hits_allowed",
    "pitching-hits-allowed": "hits_allowed",
    "earned-run": "earned_runs",
    "earned-runs": "earned_runs",
    "earned-run-allowed": "earned_runs",
    "earned-runs-allowed": "earned_runs",
    "pitcher-earned-runs": "earned_runs",
    "pitcher-earned-runs-allowed": "earned_runs",
    "walks-allowed": "walks_allowed",
    "walk-allowed": "walks_allowed",
    "pitcher-walk": "walks_allowed",
    "pitcher-walks": "walks_allowed",
    "pitcher-walks-allowed": "walks_allowed",
    "pitching-walks-allowed": "walks_allowed",
}


def normalize_mlb_prop_market_key(
    market: Any,
    *,
    scope: str | None = None,
    position: str | None = None,
    default_strikeouts_to_batter: bool = False,
) -> str:
    key = slug_key(market)
    if key in MLB_PROP_MARKET_ALIASES:
        normalized = MLB_PROP_MARKET_ALIASES[key]
    elif "hit" in key and "run" in key and ("rbi" in key or "rbis" in key):
        normalized = "hits_runs_rbis"
    elif "total" in key and "base" in key:
        normalized = "total_bases"
    elif "hit" in key and "allowed" in key:
        normalized = "hits_allowed"
    elif "earned" in key and "run" in key:
        normalized = "earned_runs"
    elif "home" in key and "run" in key:
        normalized = "home_runs"
    elif "walk" in key:
        normalized = "walks_allowed" if ("allowed" in key or "pitcher" in key) else "batter_walks"
    elif "strikeout" in key or key in {"k", "ks"}:
        if "batter" in key or "hitter" in key:
            normalized = "batter_strikeouts"
        elif "pitcher" in key or "pitching" in key or slug_key(position) == "p":
            normalized = "pitcher_strikeouts"
        elif default_strikeouts_to_batter or (slug_key(scope) == "player" and slug_key(position) != "p"):
            normalized = "batter_strikeouts"
        else:
            normalized = "strikeouts"
    elif "single" in key:
        normalized = "singles"
    elif "rbi" in key or "rbis" in key:
        normalized = "rbi"
    elif key == "runs" or key.endswith("-runs"):
        normalized = "runs"
    elif "hit" in key:
        normalized = "hits"
    else:
        normalized = key.replace("-", "_")
    return normalized
