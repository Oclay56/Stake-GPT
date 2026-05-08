from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from .stake_client import StakeAPIError


DEFAULT_TIMEZONE = "America/New_York"


async def build_slate(
    client: Any,
    sport: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    target_date = slate_date or datetime.now(timezone).date()
    schedule = await client.get_sport_schedule(sport)
    fixtures = _fixtures_for_date(schedule, target_date, timezone)
    fixtures = fixtures[: _clean_limit(limit)]

    return {
        "sport": schedule.get("sport") or {"slug": sport},
        "date": target_date.isoformat(),
        "timezone": timezone_name,
        "fixtureCount": len(fixtures),
        "fixtures": [await _fixture_with_odds(client, fixture) for fixture in fixtures],
    }


async def build_market_slate(
    client: Any,
    sport: str,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
) -> dict[str, Any]:
    raw_slate = await build_slate(
        client=client,
        sport=sport,
        slate_date=slate_date,
        timezone_name=timezone_name,
        limit=limit,
    )

    fixtures = [_clean_fixture_markets(fixture) for fixture in raw_slate["fixtures"]]

    return {
        "sport": raw_slate["sport"],
        "date": raw_slate["date"],
        "timezone": raw_slate["timezone"],
        "fixtureCount": len(fixtures),
        "marketRowCount": sum(fixture["marketCount"] for fixture in fixtures),
        "fixtures": fixtures,
    }


async def build_mlb_player_props_slate(
    client: Any,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    line_mode: str = "primary",
    include_markets: Iterable[str] | None = None,
    exclude_markets: Iterable[str] | None = None,
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    target_date = slate_date or datetime.now(timezone).date()
    schedule = await client.get_tournament_schedule("baseball", "usa", "mlb")
    fixtures = _fixtures_for_date(schedule, target_date, timezone)
    fixtures = fixtures[: _clean_limit(limit)]
    include_filter = _normalize_market_filter(include_markets)
    exclude_filter = _normalize_market_filter(exclude_markets)
    clean_fixtures = [
        await _fixture_with_player_props(
            client,
            fixture,
            line_mode,
            include_filter,
            exclude_filter,
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
        },
        "fixtureCount": len(clean_fixtures),
        "playerPropRowCount": sum(
            fixture["playerPropCount"] for fixture in clean_fixtures
        ),
        "fixtures": clean_fixtures,
    }


async def build_mlb_primary_line_check(
    client: Any,
    slate_date: date | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    limit: int = 25,
    include_markets: Iterable[str] | None = None,
    exclude_markets: Iterable[str] | None = None,
) -> dict[str, Any]:
    timezone = ZoneInfo(timezone_name)
    target_date = slate_date or datetime.now(timezone).date()
    schedule = await client.get_tournament_schedule("baseball", "usa", "mlb")
    fixtures = _fixtures_for_date(schedule, target_date, timezone)
    fixtures = fixtures[: _clean_limit(limit)]
    include_filter = _normalize_market_filter(include_markets)
    exclude_filter = _normalize_market_filter(exclude_markets)

    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fixture in fixtures:
        fixture_checks, fixture_error = await _fixture_primary_line_checks(
            client,
            fixture,
            include_filter,
            exclude_filter,
        )
        checks.extend(fixture_checks)
        if fixture_error:
            errors.append(fixture_error)

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
        },
        "fixtureCount": len(fixtures),
        "checkedPropCount": len(checks),
        "alternateLinePropCount": sum(
            1 for check in checks if check["alternateLineCount"] > 0
        ),
        "checks": checks,
        "errors": errors,
    }


def flatten_market_rows(odds: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for group in odds.get("groups") or []:
        group_name = group.get("name")
        for market in _iter_markets(group.get("markets") or []):
            for outcome in market.get("outcomes") or []:
                row = {
                    "group": group_name,
                    "market": market.get("name"),
                    "marketStatus": market.get("status"),
                    "specifiers": market.get("specifiers") or "",
                    "selection": outcome.get("name"),
                    "odds": outcome.get("odds"),
                    "active": outcome.get("active"),
                    "updatedAt": market.get("updatedAt"),
                }
                key = (
                    row["market"],
                    row["specifiers"],
                    row["selection"],
                    row["odds"],
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)

    return rows


def flatten_player_prop_rows(
    odds: dict[str, Any],
    line_mode: str = "primary",
) -> list[dict[str, Any]]:
    return _flatten_player_prop_rows(odds, line_mode=line_mode)


def flatten_all_player_prop_rows(odds: dict[str, Any]) -> list[dict[str, Any]]:
    return _flatten_player_prop_rows(odds, line_mode="all")


def select_primary_player_prop_outcome(prop: dict[str, Any]) -> dict[str, Any] | None:
    valid_outcomes = _valid_player_prop_outcomes(prop)
    if not valid_outcomes:
        return None

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


def _flatten_player_prop_rows(
    odds: dict[str, Any],
    line_mode: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for prop in _player_props_from_swish_markets(odds.get("swishMarkets")):
        outcomes = _player_prop_outcomes_for_mode(prop, line_mode)
        for outcome in outcomes:
            over = outcome.get("over")
            under = outcome.get("under")
            if not over and not under:
                continue

            row = {
                "player": repair_mojibake(prop.get("competitorName")),
                "team": repair_mojibake(prop.get("teamName")),
                "market": repair_mojibake(prop.get("marketName")),
                "sportStatType": prop.get("sportStatType"),
                "line": outcome.get("line"),
                "over": over,
                "under": under,
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


def render_market_slate_html(slate: dict[str, Any]) -> str:
    sport_name = _display_sport_name(slate.get("sport"))
    rows = []

    for fixture in slate.get("fixtures") or []:
        fixture_name = fixture.get("name") or fixture.get("slug") or ""
        if fixture.get("oddsError"):
            error = fixture["oddsError"].get("message", "odds unavailable")
            rows.append(
                "<tr>"
                f"<td>{escape(fixture_name)}</td>"
                "<td colspan=\"5\">"
                f"{escape(error)}"
                "</td>"
                "</tr>"
            )
            continue

        for market in fixture.get("marketRows") or []:
            rows.append(
                "<tr>"
                f"<td>{escape(fixture_name)}</td>"
                f"<td>{escape(str(market.get('group') or ''))}</td>"
                f"<td>{escape(str(market.get('market') or ''))}</td>"
                f"<td>{escape(str(market.get('selection') or ''))}</td>"
                f"<td>{escape(str(market.get('odds') or ''))}</td>"
                f"<td>{escape(str(market.get('specifiers') or ''))}</td>"
                "</tr>"
            )

    table_rows = "\n".join(rows) or (
        "<tr><td colspan=\"6\">No markets found for this slate.</td></tr>"
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(sport_name)} Slate</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, sans-serif;
      background: #f4f6f8;
      color: #18202a;
    }}
    body {{
      margin: 0;
      padding: 24px;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
    }}
    h1 {{
      font-size: 28px;
      margin: 0 0 8px;
    }}
    .meta {{
      color: #526170;
      margin-bottom: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: white;
      border: 1px solid #d8dee6;
    }}
    th, td {{
      border-bottom: 1px solid #e4e8ee;
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      background: #eef2f6;
      color: #26313d;
      font-weight: 700;
    }}
    tr:hover td {{
      background: #f8fafc;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escape(sport_name)} Slate</h1>
    <div class="meta">
      {escape(str(slate.get("date")))} - {escape(str(slate.get("fixtureCount")))} fixtures - {escape(str(slate.get("marketRowCount")))} market rows
    </div>
    <table>
      <thead>
        <tr>
          <th>Fixture</th>
          <th>Group</th>
          <th>Market</th>
          <th>Selection</th>
          <th>Odds</th>
          <th>Specifiers</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </main>
</body>
</html>"""


def render_player_props_html(
    slate: dict[str, Any],
    data_url: str = "/slate/mlb/player-props",
    refresh_seconds: int = 30,
) -> str:
    rows = []

    for fixture in slate.get("fixtures") or []:
        fixture_name = fixture.get("name") or fixture.get("slug") or ""
        fixture_slug = fixture.get("slug") or ""
        if fixture.get("oddsError"):
            error = fixture["oddsError"].get("message", "odds unavailable")
            rows.append(
                "<tr>"
                f"<td>{escape(fixture_name)}</td>"
                "<td colspan=\"6\">"
                f"{escape(error)}"
                "</td>"
                "</tr>"
            )
            continue

        for prop in fixture.get("playerProps") or []:
            player = str(prop.get("player") or "")
            team = str(prop.get("team") or "")
            market = str(prop.get("market") or "")
            line = str(prop.get("line") or "")
            prop_key = "|".join([str(fixture_slug), player, team, market, line])
            rows.append(
                "<tr "
                f"data-player=\"{escape(player.lower(), quote=True)}\" "
                f"data-team=\"{escape(team.lower(), quote=True)}\" "
                f"data-market=\"{escape(market.lower(), quote=True)}\" "
                f"data-prop-key=\"{escape(prop_key, quote=True)}\">"
                f"<td>{escape(fixture_name)}</td>"
                f"<td>{escape(player)}</td>"
                f"<td>{escape(team)}</td>"
                f"<td>{escape(market)}</td>"
                f"<td>{escape(line)}</td>"
                f"<td data-odds=\"over\">{escape(str(prop.get('over') or ''))}</td>"
                f"<td data-odds=\"under\">{escape(str(prop.get('under') or ''))}</td>"
                "</tr>"
            )

    table_rows = "\n".join(rows) or (
        "<tr><td colspan=\"7\">No MLB player props found for this slate.</td></tr>"
    )
    refresh_ms = max(5, min(refresh_seconds, 300)) * 1000
    escaped_data_url = escape(data_url, quote=True)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MLB Player Props</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, sans-serif;
      background: #f4f6f8;
      color: #18202a;
    }}
    body {{
      margin: 0;
      padding: 24px;
    }}
    main {{
      max-width: 1320px;
      margin: 0 auto;
    }}
    h1 {{
      font-size: 28px;
      margin: 0 0 8px;
    }}
    .meta {{
      color: #526170;
      margin-bottom: 18px;
    }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .controls input,
    .controls select,
    .controls button {{
      border: 1px solid #c9d2dc;
      border-radius: 4px;
      background: white;
      color: #18202a;
      font: inherit;
      min-height: 36px;
      padding: 7px 10px;
    }}
    .controls input {{
      min-width: 280px;
    }}
    .controls button {{
      cursor: pointer;
      font-weight: 700;
    }}
    .status {{
      color: #526170;
      font-size: 13px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: white;
      border: 1px solid #d8dee6;
    }}
    th, td {{
      border-bottom: 1px solid #e4e8ee;
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      background: #eef2f6;
      color: #26313d;
      font-weight: 700;
    }}
    tr:hover td {{
      background: #f8fafc;
    }}
    .changed {{
      background: #fff1a8;
      transition: background 1.2s ease;
    }}
    .hidden {{
      display: none;
    }}
  </style>
</head>
<body>
  <main data-board data-url="{escaped_data_url}" data-refresh-ms="{refresh_ms}">
    <h1>MLB Player Props</h1>
    <div class="meta" id="slateMeta">
      {escape(str(slate.get("date")))} - {escape(str(slate.get("fixtureCount")))} fixtures - {escape(str(slate.get("playerPropRowCount")))} player prop rows
    </div>
    <div class="controls">
      <input id="searchBox" type="search" placeholder="Search player, team, fixture, market">
      <select id="marketFilter">
        <option value="">All markets</option>
      </select>
      <button id="refreshNow" type="button">Refresh</button>
      <span class="status">
        Last updated: <span id="lastUpdated">initial load</span> -
        <span id="refreshStatus">Live</span>
      </span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Fixture</th>
          <th>Player</th>
          <th>Team</th>
          <th>Market</th>
          <th>Line</th>
          <th>Over</th>
          <th>Under</th>
        </tr>
      </thead>
      <tbody id="propsBody">
        {table_rows}
      </tbody>
    </table>
  </main>
  <script>
    const board = document.querySelector("[data-board]");
    const dataUrl = board.dataset.url;
    const refreshMs = Number(board.dataset.refreshMs);
    const previousOdds = new Map();

    function text(value) {{
      return value === null || value === undefined ? "" : String(value);
    }}

    function escapeHtml(value) {{
      return text(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function propKey(fixture, prop) {{
      return [
        fixture.slug || "",
        prop.player || "",
        prop.team || "",
        prop.market || "",
        prop.line || ""
      ].join("|");
    }}

    function buildRow(fixture, prop) {{
      const key = propKey(fixture, prop);
      const previous = previousOdds.get(key);
      const overChanged = previous && previous.over !== prop.over;
      const underChanged = previous && previous.under !== prop.under;
      previousOdds.set(key, {{ over: prop.over, under: prop.under }});
      const fixtureName = fixture.name || fixture.slug || "";
      return `<tr data-player="${{escapeHtml(prop.player).toLowerCase()}}" data-team="${{escapeHtml(prop.team).toLowerCase()}}" data-market="${{escapeHtml(prop.market).toLowerCase()}}" data-prop-key="${{escapeHtml(key)}}">`
        + `<td>${{escapeHtml(fixtureName)}}</td>`
        + `<td>${{escapeHtml(prop.player)}}</td>`
        + `<td>${{escapeHtml(prop.team)}}</td>`
        + `<td>${{escapeHtml(prop.market)}}</td>`
        + `<td>${{escapeHtml(prop.line)}}</td>`
        + `<td data-odds="over" class="${{overChanged ? "changed" : ""}}">${{escapeHtml(prop.over)}}</td>`
        + `<td data-odds="under" class="${{underChanged ? "changed" : ""}}">${{escapeHtml(prop.under)}}</td>`
        + `</tr>`;
    }}

    function syncMarketFilter(markets) {{
      const select = document.getElementById("marketFilter");
      const current = select.value;
      const options = ['<option value="">All markets</option>'];
      [...markets].sort().forEach((market) => {{
        const selected = market === current ? " selected" : "";
        options.push(`<option value="${{escapeHtml(market.toLowerCase())}}"${{selected}}>${{escapeHtml(market)}}</option>`);
      }});
      select.innerHTML = options.join("");
    }}

    function applyFilters() {{
      const needle = document.getElementById("searchBox").value.trim().toLowerCase();
      const market = document.getElementById("marketFilter").value;
      document.querySelectorAll("#propsBody tr").forEach((row) => {{
        const textMatch = !needle || row.textContent.toLowerCase().includes(needle);
        const marketMatch = !market || row.dataset.market === market;
        row.classList.toggle("hidden", !(textMatch && marketMatch));
      }});
    }}

    function renderSlate(slate) {{
      const markets = new Set();
      const rows = [];
      (slate.fixtures || []).forEach((fixture) => {{
        if (fixture.oddsError) {{
          rows.push(`<tr><td>${{escapeHtml(fixture.name || fixture.slug)}}</td><td colspan="6">${{escapeHtml(fixture.oddsError.message || "odds unavailable")}}</td></tr>`);
          return;
        }}
        (fixture.playerProps || []).forEach((prop) => {{
          if (prop.market) {{
            markets.add(prop.market);
          }}
          rows.push(buildRow(fixture, prop));
        }});
      }});
      document.getElementById("propsBody").innerHTML = rows.join("") || '<tr><td colspan="7">No MLB player props found for this slate.</td></tr>';
      document.getElementById("slateMeta").textContent = `${{slate.date}} - ${{slate.fixtureCount}} fixtures - ${{slate.playerPropRowCount}} player prop rows`;
      syncMarketFilter(markets);
      applyFilters();
      document.getElementById("lastUpdated").textContent = new Date().toLocaleTimeString();
    }}

    async function refreshBoard() {{
      const status = document.getElementById("refreshStatus");
      status.textContent = "Refreshing";
      try {{
        const response = await fetch(dataUrl, {{ cache: "no-store" }});
        if (!response.ok) {{
          throw new Error(`HTTP ${{response.status}}`);
        }}
        renderSlate(await response.json());
        status.textContent = "Live";
      }} catch (error) {{
        status.textContent = `Refresh failed: ${{error.message}}`;
      }}
    }}

    document.getElementById("searchBox").addEventListener("input", applyFilters);
    document.getElementById("marketFilter").addEventListener("change", applyFilters);
    document.getElementById("refreshNow").addEventListener("click", refreshBoard);
    setInterval(refreshBoard, {refresh_ms});
  </script>
</body>
</html>"""


async def _fixture_with_odds(client: Any, fixture: dict[str, Any]) -> dict[str, Any]:
    slug = str(fixture.get("slug") or "")
    item = {
        "slug": slug,
        "name": fixture.get("name"),
        "startTime": _fixture_start_ms(fixture),
        "status": fixture.get("status"),
        "type": fixture.get("type"),
        "odds": None,
        "oddsError": None,
    }

    if not slug:
        item["oddsError"] = {
            "statusCode": 500,
            "message": "Fixture is missing a slug.",
        }
        return item

    try:
        item["odds"] = await client.get_odds(slug)
    except StakeAPIError as exc:
        item["oddsError"] = {
            "statusCode": exc.status_code,
            "message": exc.message,
        }

    return item


async def _fixture_with_player_props(
    client: Any,
    fixture: dict[str, Any],
    line_mode: str,
    include_markets: set[str] | None = None,
    exclude_markets: set[str] | None = None,
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
        item["oddsError"] = {
            "statusCode": 500,
            "message": "Fixture is missing a slug.",
        }
        return item

    try:
        odds = await client.get_odds(slug)
    except StakeAPIError as exc:
        item["oddsError"] = {
            "statusCode": exc.status_code,
            "message": exc.message,
        }
        return item

    odds_fixture = odds.get("fixture") or {}
    player_props = _flatten_player_prop_rows(odds, line_mode)
    player_props = _filter_player_prop_rows(
        player_props,
        include_markets or set(),
        exclude_markets or set(),
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


async def _fixture_primary_line_checks(
    client: Any,
    fixture: dict[str, Any],
    include_markets: set[str],
    exclude_markets: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    slug = str(fixture.get("slug") or "")
    if not slug:
        return [], {
            "slug": "",
            "statusCode": 500,
            "message": "Fixture is missing a slug.",
        }

    try:
        odds = await client.get_odds(slug)
    except StakeAPIError as exc:
        return [], {
            "slug": slug,
            "statusCode": exc.status_code,
            "message": exc.message,
        }

    odds_fixture = odds.get("fixture") or {}
    game = repair_mojibake(
        odds_fixture.get("name") or fixture.get("name") or fixture.get("slug") or ""
    )
    checks = []
    for prop in _player_props_from_swish_markets(odds.get("swishMarkets")):
        market = repair_mojibake(prop.get("marketName"))
        market_key = _market_filter_key(market)
        if include_markets and market_key not in include_markets:
            continue
        if exclude_markets and market_key in exclude_markets:
            continue

        valid_outcomes = _valid_player_prop_outcomes(prop)
        primary = select_primary_player_prop_outcome(prop)
        if primary is None:
            continue

        checks.append(
            {
                "fixtureSlug": slug,
                "game": game,
                "player": repair_mojibake(prop.get("competitorName")),
                "team": repair_mojibake(prop.get("teamName")),
                "market": market,
                "selectedLine": primary.get("line"),
                "selectedOver": primary.get("over"),
                "selectedUnder": primary.get("under"),
                "validLineCount": len(valid_outcomes),
                "alternateLineCount": max(0, len(valid_outcomes) - 1),
                "method": "closest-over-under-balance",
                "allLines": [
                    {
                        "line": outcome.get("line"),
                        "over": outcome.get("over"),
                        "under": outcome.get("under"),
                    }
                    for outcome in valid_outcomes
                ],
            }
        )

    return checks, None


def _clean_fixture_markets(fixture: dict[str, Any]) -> dict[str, Any]:
    odds = fixture.get("odds") or {}
    odds_fixture = odds.get("fixture") or {}
    market_rows = flatten_market_rows(odds)
    start_time = odds_fixture.get("startTime") or fixture.get("startTime")

    return {
        "slug": fixture.get("slug"),
        "name": repair_mojibake(odds_fixture.get("name") or fixture.get("name")),
        "startTime": start_time,
        "status": odds_fixture.get("status") or fixture.get("status"),
        "type": odds_fixture.get("type") or fixture.get("type"),
        "marketCount": len(market_rows),
        "marketRows": market_rows,
        "oddsError": fixture.get("oddsError"),
    }


def _iter_markets(markets: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    for market in markets:
        if isinstance(market, dict):
            flattened.append(market)
        elif isinstance(market, list):
            flattened.extend(_iter_markets(market))

    return flattened


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
) -> list[dict[str, Any]]:
    if line_mode == "all":
        return [
            outcome
            for outcome in prop.get("outcomes") or []
            if outcome.get("over") or outcome.get("under")
        ]

    primary = select_primary_player_prop_outcome(prop)
    return [primary] if primary else []


def _valid_player_prop_outcomes(prop: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        outcome
        for outcome in prop.get("outcomes") or []
        if _valid_decimal_odd(outcome.get("over"))
        and _valid_decimal_odd(outcome.get("under"))
    ]


def _filter_player_prop_rows(
    rows: list[dict[str, Any]],
    include_markets: set[str],
    exclude_markets: set[str],
) -> list[dict[str, Any]]:
    if not include_markets and not exclude_markets:
        return rows

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
    return str(value or "").strip().lower()


def _repair_mojibake_text(value: str) -> str:
    if not any(marker in value for marker in ("Ã", "Â", "â")):
        return value

    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = value.encode(encoding).decode("utf-8")
        except UnicodeError:
            continue
        if repaired != value:
            return repaired

    return value


def _valid_decimal_odd(value: Any) -> bool:
    try:
        return float(value) > 1
    except (TypeError, ValueError):
        return False


def _display_sport_name(sport: Any) -> str:
    if isinstance(sport, dict):
        name = sport.get("name") or sport.get("slug")
    else:
        name = sport

    if not name:
        return "Slate"

    return str(name).replace("-", " ").title()


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


def _date_from_epoch_ms(epoch_ms: int, timezone: ZoneInfo) -> date:
    return datetime.fromtimestamp(epoch_ms / 1000, timezone).date()


def _clean_limit(limit: int) -> int:
    return max(1, min(limit, 100))
