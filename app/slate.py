from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

from .mlb_props import slug_key
from .stake_client import StakeAPIError


DEFAULT_TIMEZONE = "America/New_York"


async def build_mlb_matchups(
    client: Any,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    target_date = slate_date or datetime.now(timezone).date()
    schedule = await client.get_tournament_schedule("baseball", "usa", "mlb")
    fixtures = _fixtures_for_date(schedule, target_date, timezone)[: _clean_limit(limit)]

    return {
        "league": "MLB",
        "sport": schedule.get("sport") or {"slug": "baseball"},
        "category": "usa",
        "tournament": "mlb",
        "date": target_date.isoformat(),
        "timezone": timezone_name,
        "fixtureCount": len(fixtures),
        "matchups": [_matchup_row(fixture) for fixture in fixtures],
    }


async def build_mlb_player_props_slate(
    client: Any,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    line_mode: str = "primary",
    include_markets: Iterable[str] | None = None,
    exclude_markets: Iterable[str] | None = None,
    fixture_filter: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    target_date = slate_date or datetime.now(timezone).date()
    schedule = await client.get_tournament_schedule("baseball", "usa", "mlb")
    fixtures = _fixtures_for_date(schedule, target_date, timezone)
    if fixture_filter is not None:
        fixtures = [fixture for fixture in fixtures if fixture_filter(fixture)]
    fixtures = fixtures[: _clean_limit(limit)]

    include_filter = _normalize_market_filter(include_markets)
    exclude_filter = _normalize_market_filter(exclude_markets)
    clean_fixtures = [
        await _fixture_with_player_props(
            client=client,
            fixture=fixture,
            line_mode=line_mode,
            include_markets=include_filter,
            exclude_markets=exclude_filter,
        )
        for fixture in fixtures
    ]

    return {
        "league": "MLB",
        "sport": schedule.get("sport") or {"slug": "baseball"},
        "category": "usa",
        "tournament": "mlb",
        "date": target_date.isoformat(),
        "timezone": timezone_name,
        "filters": {
            "markets": sorted(include_filter),
            "excludeMarkets": sorted(exclude_filter),
            "lineMode": line_mode,
        },
        "fixtureCount": len(clean_fixtures),
        "playerPropRowCount": sum(
            fixture["playerPropCount"] for fixture in clean_fixtures
        ),
        "fixtures": clean_fixtures,
    }


def flatten_player_prop_rows(
    odds: dict[str, Any],
    line_mode: str = "primary",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for prop in _player_props_from_swish_markets(odds.get("swishMarkets")):
        market_name = repair_mojibake(prop.get("marketName"))
        primary = select_primary_player_prop_outcome(prop, market_name=market_name)
        outcomes = _player_prop_outcomes_for_mode(prop, line_mode, market_name)
        for outcome in outcomes:
            over = outcome.get("over")
            under = outcome.get("under")
            if not over and not under:
                continue
            is_primary = _same_outcome(primary, outcome)

            row = {
                "player": repair_mojibake(prop.get("competitorName")),
                "team": repair_mojibake(prop.get("teamName")),
                "market": market_name,
                "sportStatType": prop.get("sportStatType"),
                "line": outcome.get("line"),
                "over": over,
                "under": under,
                "lineSource": "primary" if is_primary else "alternate",
                "isPrimaryLine": is_primary,
            }
            key = (
                row["player"],
                row["team"],
                row["market"],
                row["line"],
                row["over"],
                row["under"],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    return rows


def select_primary_player_prop_outcome(
    prop: dict[str, Any],
    market_name: Any = None,
) -> dict[str, Any] | None:
    valid_outcomes = _valid_player_prop_outcomes(prop)
    if not valid_outcomes:
        return None

    market_key = _market_filter_key(market_name or prop.get("marketName"))
    if market_key in {"hits", "runs", "rbi", "home-runs", "home-run"}:
        return min(valid_outcomes, key=lambda outcome: float(outcome.get("line") or 0))
    if market_key in {"total-bases", "total-base"}:
        return min(
            valid_outcomes,
            key=lambda outcome: (
                abs(float(outcome.get("line") or 0) - 1.5),
                abs(float(outcome["over"]) - float(outcome["under"])),
            ),
        )

    return min(
        valid_outcomes,
        key=lambda outcome: abs(float(outcome["over"]) - float(outcome["under"])),
    )


def repair_mojibake(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_mojibake_text(value)
    if isinstance(value, list):
        return [repair_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_mojibake(item) for key, item in value.items()}
    return value


async def _fixture_with_player_props(
    client: Any,
    fixture: dict[str, Any],
    line_mode: str,
    include_markets: set[str],
    exclude_markets: set[str],
) -> dict[str, Any]:
    slug = str(fixture.get("slug") or "")
    item = {
        "slug": slug,
        "name": repair_mojibake(fixture.get("name")),
        "startTime": _fixture_start_ms(fixture),
        "status": fixture.get("status"),
        "type": fixture.get("type"),
        "playerPropCount": 0,
        "playerProps": [],
        "oddsError": None,
    }
    if not slug:
        item["oddsError"] = {"statusCode": 500, "message": "Fixture is missing a slug."}
        return item

    try:
        odds = await client.get_odds(slug)
    except StakeAPIError as exc:
        item["oddsError"] = {"statusCode": exc.status_code, "message": exc.message}
        return item

    odds_fixture = odds.get("fixture") or {}
    player_props = flatten_player_prop_rows(odds, line_mode=line_mode)
    player_props = _filter_player_prop_rows(
        rows=player_props,
        include_markets=include_markets,
        exclude_markets=exclude_markets,
    )
    item.update(
        {
            "name": repair_mojibake(odds_fixture.get("name") or item["name"]),
            "startTime": odds_fixture.get("startTime") or item["startTime"],
            "status": odds_fixture.get("status") or item["status"],
            "type": odds_fixture.get("type") or item["type"],
            "playerPropCount": len(player_props),
            "playerProps": player_props,
        }
    )
    return item


def _matchup_row(fixture: dict[str, Any]) -> dict[str, Any]:
    name = repair_mojibake(fixture.get("name") or fixture.get("slug") or "")
    return {
        "fixtureSlug": fixture.get("slug"),
        "name": name,
        "teams": _fixture_teams(name),
        "startTime": _fixture_start_ms(fixture),
        "status": fixture.get("status"),
        "type": fixture.get("type"),
        "source": "stake_odds_api",
    }


def _player_props_from_swish_markets(swish_markets: Any) -> list[dict[str, Any]]:
    if isinstance(swish_markets, dict):
        return list(swish_markets.get("playerProps") or [])

    props: list[dict[str, Any]] = []
    if isinstance(swish_markets, list):
        for item in swish_markets:
            if isinstance(item, dict):
                props.extend(item.get("playerProps") or [])
    return props


def _player_prop_outcomes_for_mode(
    prop: dict[str, Any],
    line_mode: str,
    market_name: Any,
) -> list[dict[str, Any]]:
    if line_mode == "all":
        return [
            outcome
            for outcome in prop.get("outcomes") or []
            if outcome.get("over") or outcome.get("under")
        ]

    primary = select_primary_player_prop_outcome(prop, market_name=market_name)
    return [primary] if primary else []


def _valid_player_prop_outcomes(prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        outcome
        for outcome in prop.get("outcomes") or []
        if outcome.get("line") is not None
        and _valid_decimal_odd(outcome.get("over"))
        and _valid_decimal_odd(outcome.get("under"))
    ]


def _filter_player_prop_rows(
    rows: list[dict[str, Any]],
    include_markets: set[str],
    exclude_markets: set[str],
) -> list[dict[str, Any]]:
    filtered_rows = []
    for row in rows:
        market = _market_filter_key(row.get("market"))
        if include_markets and market not in include_markets:
            continue
        if exclude_markets and market in exclude_markets:
            continue
        filtered_rows.append(row)
    return filtered_rows


def _normalize_market_filter(markets: Iterable[str] | None) -> set[str]:
    if not markets:
        return set()
    return {
        _market_filter_key(market)
        for market in markets
        if _market_filter_key(market)
    }


def _market_filter_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "-")


def _fixtures_for_date(
    schedule: dict[str, Any],
    target_date: date,
    timezone: ZoneInfo,
) -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = []
    for schedule_item in schedule.get("schedule") or []:
        schedule_date_ms = schedule_item.get("date")
        for fixture in schedule_item.get("fixtures") or []:
            fixture_date_ms = _fixture_start_ms(fixture) or schedule_date_ms
            if fixture_date_ms is None:
                continue
            if _date_from_epoch_ms(fixture_date_ms, timezone) == target_date:
                fixtures.append(fixture)
    return fixtures


def _fixture_start_ms(fixture: dict[str, Any]) -> int | None:
    value = fixture.get("startTime", fixture.get("date"))
    if value is None:
        return None
    return int(value)


def _fixture_teams(game: str) -> list[str]:
    if " - " not in game:
        return _fixture_teams_from_slug(game)
    return [part.strip() for part in game.split(" - ", 1) if part.strip()]


MLB_TEAM_NAMES_BY_SLUG = {
    "arizona-diamondbacks": "Arizona Diamondbacks",
    "atlanta-braves": "Atlanta Braves",
    "baltimore-orioles": "Baltimore Orioles",
    "boston-red-sox": "Boston Red Sox",
    "chicago-cubs": "Chicago Cubs",
    "chicago-white-sox": "Chicago White Sox",
    "cincinnati-reds": "Cincinnati Reds",
    "cleveland-guardians": "Cleveland Guardians",
    "colorado-rockies": "Colorado Rockies",
    "detroit-tigers": "Detroit Tigers",
    "houston-astros": "Houston Astros",
    "kansas-city-royals": "Kansas City Royals",
    "los-angeles-angels": "Los Angeles Angels",
    "los-angeles-dodgers": "Los Angeles Dodgers",
    "miami-marlins": "Miami Marlins",
    "milwaukee-brewers": "Milwaukee Brewers",
    "minnesota-twins": "Minnesota Twins",
    "new-york-mets": "New York Mets",
    "new-york-yankees": "New York Yankees",
    "athletics": "Athletics",
    "oakland-athletics": "Oakland Athletics",
    "philadelphia-phillies": "Philadelphia Phillies",
    "pittsburgh-pirates": "Pittsburgh Pirates",
    "san-diego-padres": "San Diego Padres",
    "san-francisco-giants": "San Francisco Giants",
    "seattle-mariners": "Seattle Mariners",
    "st-louis-cardinals": "St. Louis Cardinals",
    "tampa-bay-rays": "Tampa Bay Rays",
    "texas-rangers": "Texas Rangers",
    "toronto-blue-jays": "Toronto Blue Jays",
    "washington-nationals": "Washington Nationals",
}


def _fixture_teams_from_slug(value: str) -> list[str]:
    slug = re.sub(r"^\d+-", "", slug_key(value))
    if not slug:
        return []

    team_slugs = sorted(MLB_TEAM_NAMES_BY_SLUG, key=len, reverse=True)
    for away_slug in team_slugs:
        prefix = f"{away_slug}-"
        if not slug.startswith(prefix):
            continue
        home_slug = slug.removeprefix(prefix)
        home_name = MLB_TEAM_NAMES_BY_SLUG.get(home_slug)
        if home_name:
            return [MLB_TEAM_NAMES_BY_SLUG[away_slug], home_name]
    return []


def _date_from_epoch_ms(epoch_ms: int, timezone: ZoneInfo) -> date:
    return datetime.fromtimestamp(epoch_ms / 1000, timezone).date()


def _valid_decimal_odd(value: Any) -> bool:
    try:
        return float(value) > 1
    except (TypeError, ValueError):
        return False


def _same_outcome(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not left or not right:
        return False
    return (
        left.get("line") == right.get("line")
        and left.get("over") == right.get("over")
        and left.get("under") == right.get("under")
    )


def _repair_mojibake_text(value: str) -> str:
    if not any(marker in value for marker in ("Ãƒ", "Ã‚", "Ã¢")):
        return value
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        if repaired != value:
            return repaired
    return value


def _clean_limit(limit: int) -> int:
    return max(1, min(limit, 100))
