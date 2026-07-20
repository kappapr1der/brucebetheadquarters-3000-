from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from .storage import upsert_match_odds


API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_ODDS_SPORT = "soccer_epl"
DEFAULT_ODDS_REGIONS = "eu"
DEFAULT_ODDS_MARKETS = "h2h,totals"
DEFAULT_ODDS_BOOKMAKER = "market_avg"


TEAM_ALIASES = {
    "afc bournemouth": "Bournemouth",
    "bournemouth": "Bournemouth",
    "arsenal": "Arsenal",
    "aston villa": "Aston Villa",
    "brentford": "Brentford",
    "brighton": "Brighton",
    "brighton and hove albion": "Brighton",
    "brighton hove albion": "Brighton",
    "burnley": "Burnley",
    "chelsea": "Chelsea",
    "crystal palace": "Crystal Palace",
    "everton": "Everton",
    "fulham": "Fulham",
    "leeds": "Leeds",
    "leeds united": "Leeds",
    "liverpool": "Liverpool",
    "manchester city": "Manchester City",
    "man city": "Manchester City",
    "manchester united": "Manchester United",
    "man united": "Manchester United",
    "newcastle": "Newcastle",
    "newcastle united": "Newcastle",
    "nottingham forest": "Nottingham Forest",
    "sunderland": "Sunderland",
    "tottenham": "Tottenham",
    "tottenham hotspur": "Tottenham",
    "west ham": "West Ham",
    "west ham united": "West Ham",
    "wolverhampton": "Wolves",
    "wolverhampton wanderers": "Wolves",
    "wolves": "Wolves",
}


@dataclass(frozen=True)
class OddsQuota:
    requests_remaining: int | None
    requests_used: int | None
    requests_last: int | None


@dataclass(frozen=True)
class SportsCheck:
    ok: bool
    sports_count: int
    sport_keys: list[str]
    quota: OddsQuota


@dataclass(frozen=True)
class OddsSnapshot:
    home_win: float | None = None
    draw: float | None = None
    away_win: float | None = None
    over_2_5: float | None = None
    under_2_5: float | None = None
    btts_yes: float | None = None
    btts_no: float | None = None
    bookmaker_count: int = 0


@dataclass(frozen=True)
class OddsEvent:
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str
    snapshot: OddsSnapshot
    raw_bookmaker_count: int


@dataclass(frozen=True)
class OddsImportResult:
    sport: str
    regions: str
    markets: str
    bookmaker: str
    captured_at: str
    events_seen: int
    matched: int
    inserted: int
    unmatched: list[str] = field(default_factory=list)
    quota: OddsQuota = field(default_factory=lambda: OddsQuota(None, None, None))


class OddsApiError(RuntimeError):
    pass


def _header_int(headers: object, name: str) -> int | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def quota_from_headers(headers: object) -> OddsQuota:
    return OddsQuota(
        requests_remaining=_header_int(headers, "x-requests-remaining"),
        requests_used=_header_int(headers, "x-requests-used"),
        requests_last=_header_int(headers, "x-requests-last"),
    )


def _clean_key(value: str) -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def canonical_team_name(name: str) -> str:
    cleaned = _clean_key(name)
    return TEAM_ALIASES.get(cleaned, name.strip())


def match_pair_key(home: str, away: str) -> tuple[str, str]:
    return (_clean_key(canonical_team_name(home)), _clean_key(canonical_team_name(away)))


def parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _outcome_name(outcome: dict[str, object]) -> str:
    return str(outcome.get("name") or "").strip()


def average_event_odds(event: dict[str, object]) -> OddsSnapshot:
    home = str(event.get("home_team") or "")
    away = str(event.get("away_team") or "")
    home_key = _clean_key(home)
    away_key = _clean_key(away)
    home_prices: list[float] = []
    draw_prices: list[float] = []
    away_prices: list[float] = []
    over_prices: list[float] = []
    under_prices: list[float] = []
    btts_yes_prices: list[float] = []
    btts_no_prices: list[float] = []
    bookmaker_count = 0

    for bookmaker in event.get("bookmakers") or []:
        if not isinstance(bookmaker, dict):
            continue
        bookmaker_count += 1
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, dict):
                continue
            market_key = str(market.get("key") or "").strip().lower()
            for outcome in market.get("outcomes") or []:
                if not isinstance(outcome, dict) or outcome.get("price") in (None, ""):
                    continue
                try:
                    price = float(outcome["price"])
                except (TypeError, ValueError):
                    continue
                name = _outcome_name(outcome)
                name_key = _clean_key(name)

                if market_key == "h2h":
                    if name_key == home_key:
                        home_prices.append(price)
                    elif name_key == away_key:
                        away_prices.append(price)
                    elif name_key == "draw":
                        draw_prices.append(price)
                elif market_key == "totals":
                    try:
                        point = float(outcome.get("point"))
                    except (TypeError, ValueError):
                        point = None
                    if point == 2.5 and name_key == "over":
                        over_prices.append(price)
                    elif point == 2.5 and name_key == "under":
                        under_prices.append(price)
                elif market_key in {"btts", "both_teams_to_score"}:
                    if name_key == "yes":
                        btts_yes_prices.append(price)
                    elif name_key == "no":
                        btts_no_prices.append(price)

    return OddsSnapshot(
        home_win=_mean(home_prices),
        draw=_mean(draw_prices),
        away_win=_mean(away_prices),
        over_2_5=_mean(over_prices),
        under_2_5=_mean(under_prices),
        btts_yes=_mean(btts_yes_prices),
        btts_no=_mean(btts_no_prices),
        bookmaker_count=bookmaker_count,
    )


class TheOddsApiClient:
    def __init__(self, api_key: str, base_url: str = API_BASE, timeout: int = 20) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise OddsApiError("THE_ODDS_API_KEY is required")

    def _get(self, path: str, params: dict[str, str | int | None]) -> tuple[object, OddsQuota]:
        clean_params = {"apiKey": self.api_key}
        clean_params.update({key: value for key, value in params.items() if value not in (None, "")})
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(clean_params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return payload, quota_from_headers(response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise OddsApiError(f"The Odds API HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise OddsApiError(f"The Odds API request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise OddsApiError("The Odds API returned invalid JSON") from exc

    def sports(self, all_sports: bool = False) -> SportsCheck:
        payload, quota = self._get("/sports/", {"all": "true" if all_sports else None})
        if not isinstance(payload, list):
            raise OddsApiError("Unexpected /sports response")
        return SportsCheck(
            ok=True,
            sports_count=len(payload),
            sport_keys=[str(item.get("key")) for item in payload if isinstance(item, dict) and item.get("key")],
            quota=quota,
        )

    def odds(
        self,
        sport: str = DEFAULT_ODDS_SPORT,
        regions: str = DEFAULT_ODDS_REGIONS,
        markets: str = DEFAULT_ODDS_MARKETS,
        commence_time_from: str | None = None,
        commence_time_to: str | None = None,
    ) -> tuple[list[OddsEvent], OddsQuota]:
        payload, quota = self._get(
            f"/sports/{sport}/odds/",
            {
                "regions": regions,
                "markets": markets,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
                "commenceTimeFrom": commence_time_from,
                "commenceTimeTo": commence_time_to,
            },
        )
        if not isinstance(payload, list):
            raise OddsApiError("Unexpected /odds response")
        events: list[OddsEvent] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            events.append(
                OddsEvent(
                    event_id=str(item.get("id") or ""),
                    sport_key=str(item.get("sport_key") or sport),
                    commence_time=str(item.get("commence_time") or ""),
                    home_team=str(item.get("home_team") or ""),
                    away_team=str(item.get("away_team") or ""),
                    snapshot=average_event_odds(item),
                    raw_bookmaker_count=len(item.get("bookmakers") or []),
                )
            )
        return events, quota


def load_match_index(conn: sqlite3.Connection) -> dict[tuple[str, str], list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT m.*, r.name AS round_name
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        JOIN seasons s ON s.id = r.season_id
        WHERE s.active = 1
        ORDER BY m.kickoff_at IS NULL, m.kickoff_at, r.sort_order, m.position
        """
    ).fetchall()
    index: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        index.setdefault(match_pair_key(row["home"], row["away"]), []).append(row)
    return index


def _time_distance_seconds(left: str | None, right: str | None) -> float | None:
    left_dt = parse_iso_utc(left)
    right_dt = parse_iso_utc(right)
    if left_dt is None or right_dt is None:
        return None
    return abs((left_dt - right_dt).total_seconds())


def choose_local_match(event: OddsEvent, index: dict[tuple[str, str], list[sqlite3.Row]]) -> sqlite3.Row | None:
    candidates = index.get(match_pair_key(event.home_team, event.away_team), [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    timed = [
        (distance, row)
        for row in candidates
        if (distance := _time_distance_seconds(row["kickoff_at"], event.commence_time)) is not None
    ]
    if timed:
        timed.sort(key=lambda item: item[0])
        return timed[0][1]
    return candidates[0]


def _odds_row(match: sqlite3.Row, event: OddsEvent, captured_at: str, bookmaker: str) -> dict[str, str]:
    snapshot = event.snapshot
    notes = (
        f"The Odds API event={event.event_id}; sport={event.sport_key}; "
        f"api_home={event.home_team}; api_away={event.away_team}; bookmakers={snapshot.bookmaker_count}"
    )
    return {
        "round": str(match["round_name"]),
        "position": str(match["position"]),
        "bookmaker": bookmaker,
        "captured_at": captured_at,
        "home_win": "" if snapshot.home_win is None else str(snapshot.home_win),
        "draw": "" if snapshot.draw is None else str(snapshot.draw),
        "away_win": "" if snapshot.away_win is None else str(snapshot.away_win),
        "over_2_5": "" if snapshot.over_2_5 is None else str(snapshot.over_2_5),
        "under_2_5": "" if snapshot.under_2_5 is None else str(snapshot.under_2_5),
        "btts_yes": "" if snapshot.btts_yes is None else str(snapshot.btts_yes),
        "btts_no": "" if snapshot.btts_no is None else str(snapshot.btts_no),
        "notes": notes,
    }


def sync_odds_to_db(
    conn: sqlite3.Connection,
    api_key: str,
    sport: str = DEFAULT_ODDS_SPORT,
    regions: str = DEFAULT_ODDS_REGIONS,
    markets: str = DEFAULT_ODDS_MARKETS,
    bookmaker: str = DEFAULT_ODDS_BOOKMAKER,
    days_ahead: int = 30,
    captured_at: str | None = None,
) -> OddsImportResult:
    now = datetime.now(timezone.utc)
    captured = captured_at or now.isoformat()
    client = TheOddsApiClient(api_key)
    events, quota = client.odds(
        sport=sport,
        regions=regions,
        markets=markets,
        commence_time_from=now.isoformat().replace("+00:00", "Z"),
        commence_time_to=(now + timedelta(days=days_ahead)).isoformat().replace("+00:00", "Z"),
    )
    index = load_match_index(conn)
    unmatched: list[str] = []
    inserted = 0
    matched = 0

    for event in events:
        match = choose_local_match(event, index)
        if match is None:
            unmatched.append(f"{event.home_team} - {event.away_team} ({event.commence_time})")
            continue
        matched += 1
        upsert_match_odds(conn, _odds_row(match, event, captured, bookmaker))
        inserted += 1
    conn.commit()
    return OddsImportResult(
        sport=sport,
        regions=regions,
        markets=markets,
        bookmaker=bookmaker,
        captured_at=captured,
        events_seen=len(events),
        matched=matched,
        inserted=inserted,
        unmatched=unmatched,
        quota=quota,
    )
