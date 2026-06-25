from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import math
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from .scoring import parse_datetime
from .storage import (
    active_season_id,
    ensure_team,
    upsert_match_assessment,
    upsert_match_context,
    upsert_player_status,
    upsert_team_match_factor,
)


FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
CLUBELO_BASE_URL = "http://api.clubelo.com"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEZONE = "Europe/Moscow"


@dataclass(frozen=True)
class VariableSyncResult:
    updated_at: str
    fpl_players_seen: int = 0
    fpl_players_imported: int = 0
    fpl_teams_matched: int = 0
    fpl_unmatched_teams: tuple[str, ...] = ()
    elo_teams_checked: int = 0
    elo_teams_updated: int = 0
    elo_unmatched_teams: tuple[str, ...] = ()
    contexts_upserted: int = 0
    factors_upserted: int = 0
    weather_checked: int = 0
    weather_updated: int = 0
    weather_skipped: int = 0
    assessments_upserted: int = 0
    errors: tuple[str, ...] = ()


class VariableSyncError(RuntimeError):
    pass


TEAM_ALIASES = {
    "afc bournemouth": "Bournemouth",
    "bournemouth": "Bournemouth",
    "arsenal": "Arsenal",
    "aston villa": "Aston Villa",
    "brentford": "Brentford",
    "brighton": "Brighton and Hove Albion",
    "brighton and hove albion": "Brighton and Hove Albion",
    "brighton hove albion": "Brighton and Hove Albion",
    "chelsea": "Chelsea",
    "coventry": "Coventry City",
    "coventry city": "Coventry City",
    "crystal palace": "Crystal Palace",
    "everton": "Everton",
    "fulham": "Fulham",
    "hull": "Hull City",
    "hull city": "Hull City",
    "ipswich": "Ipswich Town",
    "ipswich town": "Ipswich Town",
    "leeds": "Leeds United",
    "leeds united": "Leeds United",
    "liverpool": "Liverpool",
    "man city": "Manchester City",
    "manchester city": "Manchester City",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "manchester utd": "Manchester United",
    "manchester united": "Manchester United",
    "newcastle": "Newcastle United",
    "newcastle united": "Newcastle United",
    "nott m forest": "Nottingham Forest",
    "nottm forest": "Nottingham Forest",
    "nottingham forest": "Nottingham Forest",
    "sunderland": "Sunderland",
    "tottenham": "Tottenham Hotspur",
    "tottenham hotspur": "Tottenham Hotspur",
    "spurs": "Tottenham Hotspur",
}

CLUBELO_ALIASES = {
    "Aston Villa": "AstonVilla",
    "Bournemouth": "Bournemouth",
    "Brighton and Hove Albion": "Brighton",
    "Coventry City": "Coventry",
    "Crystal Palace": "CrystalPalace",
    "Hull City": "Hull",
    "Ipswich Town": "Ipswich",
    "Leeds United": "Leeds",
    "Manchester City": "ManCity",
    "Manchester United": "ManUnited",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Forest",
    "Tottenham Hotspur": "Tottenham",
}

STADIUMS = {
    "Arsenal": ("Emirates Stadium", "London", 51.5549, -0.1084),
    "Aston Villa": ("Villa Park", "Birmingham", 52.5092, -1.8848),
    "Bournemouth": ("Vitality Stadium", "Bournemouth", 50.7352, -1.8383),
    "Brentford": ("Gtech Community Stadium", "London", 51.4908, -0.2887),
    "Brighton and Hove Albion": ("Amex Stadium", "Brighton", 50.8618, -0.0833),
    "Chelsea": ("Stamford Bridge", "London", 51.4816, -0.1910),
    "Coventry City": ("Coventry Building Society Arena", "Coventry", 52.4481, -1.4956),
    "Crystal Palace": ("Selhurst Park", "London", 51.3983, -0.0855),
    "Everton": ("Everton Stadium", "Liverpool", 53.4255, -2.9919),
    "Fulham": ("Craven Cottage", "London", 51.4750, -0.2217),
    "Hull City": ("MKM Stadium", "Hull", 53.7463, -0.3679),
    "Ipswich Town": ("Portman Road", "Ipswich", 52.0550, 1.1450),
    "Leeds United": ("Elland Road", "Leeds", 53.7778, -1.5722),
    "Liverpool": ("Anfield", "Liverpool", 53.4308, -2.9608),
    "Manchester City": ("Etihad Stadium", "Manchester", 53.4831, -2.2004),
    "Manchester United": ("Old Trafford", "Manchester", 53.4631, -2.2913),
    "Newcastle United": ("St James' Park", "Newcastle upon Tyne", 54.9756, -1.6217),
    "Nottingham Forest": ("City Ground", "Nottingham", 52.9400, -1.1328),
    "Sunderland": ("Stadium of Light", "Sunderland", 54.9144, -1.3882),
    "Tottenham Hotspur": ("Tottenham Hotspur Stadium", "London", 51.6043, -0.0662),
}


def _request_json(url: str, timeout: int = 30) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "BruceBetHQ/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_text(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "BruceBetHQ/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _clean_key(value: str) -> str:
    text = value.lower().replace("&", " and ")
    text = re.sub(r"\bfc\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_team_name(name: str) -> str:
    value = name.strip()
    return TEAM_ALIASES.get(_clean_key(value), value)


def _active_match_team_names(conn: sqlite3.Connection) -> list[str]:
    season_id = active_season_id(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT name
        FROM (
            SELECT m.home AS name
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            UNION
            SELECT m.away AS name
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
        )
        ORDER BY name
        """,
        (season_id, season_id),
    ).fetchall()
    if rows:
        return [row["name"] for row in rows]
    return [row["name"] for row in conn.execute("SELECT name FROM teams ORDER BY name")]


def _team_resolver(conn: sqlite3.Connection) -> dict[str, str]:
    resolver: dict[str, str] = {}
    for name in _active_match_team_names(conn):
        canonical = canonical_team_name(name)
        resolver[_clean_key(name)] = name
        resolver[_clean_key(canonical)] = name
    for alias_key, canonical in TEAM_ALIASES.items():
        if _clean_key(canonical) in resolver:
            resolver[alias_key] = resolver[_clean_key(canonical)]
    return resolver


def resolve_existing_team(conn: sqlite3.Connection, external_name: str) -> str | None:
    resolver = _team_resolver(conn)
    if not resolver:
        return canonical_team_name(external_name)
    return resolver.get(_clean_key(external_name)) or resolver.get(_clean_key(canonical_team_name(external_name)))


def fpl_status(code: object, availability_pct: float | None) -> str:
    normalized = str(code or "").lower()
    if normalized in {"i", "n", "u"}:
        return "injured" if normalized == "i" else "unavailable"
    if normalized == "s":
        return "suspended"
    if normalized == "d":
        return "doubtful"
    if availability_pct is not None and availability_pct < 100:
        return "doubtful"
    return "available"


def import_fpl_bootstrap(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    updated_at: str,
) -> tuple[int, int, int, tuple[str, ...]]:
    teams = payload.get("teams") or []
    elements = payload.get("elements") or []
    element_types = payload.get("element_types") or []
    if not isinstance(teams, list) or not isinstance(elements, list):
        raise VariableSyncError("FPL bootstrap payload is missing teams/elements")

    teams_by_id: dict[int, str] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_id = _as_int(team.get("id"))
        if team_id is not None:
            teams_by_id[team_id] = str(team.get("name") or team.get("short_name") or "")

    roles_by_id: dict[int, str] = {}
    if isinstance(element_types, list):
        for role in element_types:
            if not isinstance(role, dict):
                continue
            role_id = _as_int(role.get("id"))
            if role_id is not None:
                roles_by_id[role_id] = str(role.get("singular_name_short") or role.get("singular_name") or "")

    imported = 0
    matched_teams: set[str] = set()
    unmatched: set[str] = set()
    for item in elements:
        if not isinstance(item, dict):
            continue
        raw_team = teams_by_id.get(_as_int(item.get("team")) or -1, "")
        team_name = resolve_existing_team(conn, raw_team)
        if not team_name:
            if raw_team:
                unmatched.add(raw_team)
            continue
        chance_next = _as_float(item.get("chance_of_playing_next_round"))
        chance_this = _as_float(item.get("chance_of_playing_this_round"))
        availability = chance_next if chance_next is not None else chance_this
        if availability is None:
            availability = 100.0 if str(item.get("status") or "").lower() == "a" else 0.0
        first_name = str(item.get("first_name") or "").strip()
        second_name = str(item.get("second_name") or "").strip()
        web_name = str(item.get("web_name") or "").strip()
        player = web_name or " ".join(part for part in [first_name, second_name] if part).strip()
        if not player:
            continue
        role = roles_by_id.get(_as_int(item.get("element_type")) or -1)
        notes = []
        if item.get("news"):
            notes.append(str(item.get("news")))
        for key in ["selected_by_percent", "minutes", "points_per_game", "now_cost"]:
            if item.get(key) not in {None, ""}:
                notes.append(f"{key}={item.get(key)}")
        upsert_player_status(
            conn,
            {
                "team": team_name,
                "player": player,
                "role": role or "",
                "status": fpl_status(item.get("status"), availability),
                "availability_pct": str(round(availability, 1)),
                "form_rating": str(_as_float(item.get("form")) or ""),
                "source": "FPL",
                "source_ref": f"fpl:{item.get('id')}",
                "notes": "; ".join(notes),
                "updated_at": updated_at,
            },
        )
        imported += 1
        matched_teams.add(team_name)
    conn.commit()
    return len(elements), imported, len(matched_teams), tuple(sorted(unmatched))


def sync_fpl_player_statuses(conn: sqlite3.Connection, updated_at: str, timeout: int = 30) -> tuple[int, int, int, tuple[str, ...]]:
    payload = _request_json(FPL_BOOTSTRAP_URL, timeout=timeout)
    if not isinstance(payload, dict):
        raise VariableSyncError("FPL bootstrap returned unexpected payload")
    return import_fpl_bootstrap(conn, payload, updated_at)


def _parse_clubelo(text: str) -> tuple[float, str | None] | None:
    reader = csv.DictReader(io.StringIO(text))
    latest: tuple[float, str | None] | None = None
    for row in reader:
        normalized = {str(key).lower(): value for key, value in row.items()}
        elo = _as_float(normalized.get("elo"))
        if elo is None:
            continue
        country = str(normalized.get("country") or "").strip() or None
        latest = (elo, country)
    return latest


def sync_clubelo_teams(conn: sqlite3.Connection, updated_at: str, timeout: int = 30) -> tuple[int, int, tuple[str, ...]]:
    checked = 0
    updated = 0
    unmatched: list[str] = []
    for team in _active_match_team_names(conn):
        checked += 1
        query = CLUBELO_ALIASES.get(team, team)
        url = f"{CLUBELO_BASE_URL}/{urllib.parse.quote(query)}"
        try:
            parsed = _parse_clubelo(_request_text(url, timeout=timeout))
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, csv.Error):
            parsed = None
        if parsed is None:
            unmatched.append(team)
            continue
        elo, country = parsed
        ensure_team(conn, team, elo_rating=round(elo, 1), country=country, updated_at=updated_at)
        updated += 1
    conn.commit()
    return checked, updated, tuple(unmatched)


def _match_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    season_id = active_season_id(conn)
    return list(
        conn.execute(
            """
            SELECT m.*, r.name AS round_name, r.sort_order
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
              AND m.kickoff_at IS NOT NULL
            ORDER BY m.kickoff_at, r.sort_order, m.position
            """,
            (season_id,),
        )
    )


def _aware(dt: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(timezone_name))
    return dt


def _within_horizon(kickoff: datetime, now: datetime, days_ahead: int) -> bool:
    comparable = _aware(kickoff).astimezone(now.tzinfo)
    return now <= comparable <= now + timedelta(days=days_ahead)


def _rest_days(previous: datetime | None, current: datetime) -> int | None:
    if previous is None:
        return None
    return max(0, int((current - previous).total_seconds() // 86400))


def _nearest_hour_weather(payload: object, kickoff: datetime) -> tuple[str | None, float | None]:
    if not isinstance(payload, dict):
        return None, None
    hourly = payload.get("hourly") or {}
    if not isinstance(hourly, dict):
        return None, None
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    precip = hourly.get("precipitation") or []
    wind = hourly.get("wind_speed_10m") or []
    if not isinstance(times, list) or not times:
        return None, None
    target = kickoff.astimezone(ZoneInfo("Europe/London")).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    best_index = min(
        range(len(times)),
        key=lambda index: abs((datetime.fromisoformat(str(times[index])) - target).total_seconds()),
    )
    temp = _as_float(temps[best_index] if best_index < len(temps) else None)
    rain = _as_float(precip[best_index] if best_index < len(precip) else None)
    wind_speed = _as_float(wind[best_index] if best_index < len(wind) else None)
    parts = []
    if temp is not None:
        parts.append(f"temp={round(temp, 1)}C")
    if rain is not None:
        parts.append(f"rain={round(rain, 1)}mm")
    if wind_speed is not None:
        parts.append(f"wind={round(wind_speed, 1)}km/h")
    return ", ".join(parts) if parts else None, temp


def _fetch_weather(lat: float, lon: float, kickoff: datetime, forecast_days: int, timeout: int) -> tuple[str | None, float | None]:
    params = {
        "latitude": str(lat),
        "longitude": str(lon),
        "hourly": "temperature_2m,precipitation,wind_speed_10m",
        "timezone": "Europe/London",
        "forecast_days": str(max(1, min(16, forecast_days))),
    }
    payload = _request_json(f"{OPEN_METEO_URL}?{urllib.parse.urlencode(params)}", timeout=timeout)
    return _nearest_hour_weather(payload, kickoff)


def _latest_team_status_rows(conn: sqlite3.Connection, team_name: str) -> list[sqlite3.Row]:
    row = conn.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (team_name,)).fetchone()
    if row is None:
        return []
    return list(
        conn.execute(
            """
            SELECT ps.*
            FROM player_status_snapshots ps
            JOIN (
                SELECT team_id, player, MAX(updated_at) AS updated_at
                FROM player_status_snapshots
                WHERE team_id = ?
                GROUP BY team_id, player
            ) latest
              ON latest.team_id = ps.team_id
             AND latest.player = ps.player
             AND latest.updated_at = ps.updated_at
            ORDER BY ps.availability_pct ASC NULLS LAST, ps.form_rating DESC NULLS LAST
            """,
            (int(row["id"]),),
        )
    )


def _availability_impact(conn: sqlite3.Connection, team_name: str) -> float:
    impact = 0.0
    for row in _latest_team_status_rows(conn, team_name)[:8]:
        status = str(row["status"] or "").lower()
        availability = _as_float(row["availability_pct"])
        if status == "unavailable":
            # FPL often uses "unavailable" for registration/loan/old-squad noise.
            # Keep it visible in variables, but do not treat it like a core injury.
            player_impact = 0.15
        elif availability is not None:
            player_impact = max(0.0, min(1.0, (100.0 - availability) / 100.0))
        elif status in {"injured", "suspended", "out"}:
            player_impact = 1.0
        elif status in {"doubtful", "questionable"}:
            player_impact = 0.5
        else:
            player_impact = 0.0
        role = str(row["role"] or "").upper()
        role_weight = 1.15 if role in {"FWD", "MID"} else 1.0
        impact += player_impact * role_weight * 0.09
    return round(min(1.0, impact), 3)


def _fatigue(rest_days: int | None) -> float | None:
    if rest_days is None:
        return None
    if rest_days <= 2:
        return 0.85
    if rest_days == 3:
        return 0.55
    if rest_days == 4:
        return 0.35
    if rest_days <= 6:
        return 0.18
    return 0.08


def sync_match_contexts_and_factors(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    days_ahead: int = 365,
    weather_days: int = 16,
    timeout: int = 30,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> tuple[int, int, int, int, int]:
    current = _aware(now or datetime.now(ZoneInfo(timezone_name)), timezone_name)
    last_seen: dict[str, datetime] = {}
    contexts = 0
    factors = 0
    weather_checked = 0
    weather_updated = 0
    weather_skipped = 0
    weather_cache: dict[tuple[str, str], tuple[str | None, float | None]] = {}
    for match in _match_rows(conn):
        kickoff = _aware(parse_datetime(match["kickoff_at"]), timezone_name)
        home = str(match["home"])
        away = str(match["away"])
        home_rest = _rest_days(last_seen.get(home), kickoff)
        away_rest = _rest_days(last_seen.get(away), kickoff)
        last_seen[home] = kickoff
        last_seen[away] = kickoff
        if not _within_horizon(kickoff, current, days_ahead):
            continue

        stadium = STADIUMS.get(canonical_team_name(home))
        weather = None
        temperature = None
        notes = ["auto_context"]
        if stadium and weather_days > 0:
            weather_delta = kickoff.astimezone(current.tzinfo) - current
            if timedelta(0) <= weather_delta <= timedelta(days=min(16, weather_days)):
                weather_checked += 1
                cache_key = (home, kickoff.date().isoformat())
                if cache_key not in weather_cache:
                    try:
                        weather_cache[cache_key] = _fetch_weather(stadium[2], stadium[3], kickoff, weather_days, timeout)
                    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError):
                        weather_cache[cache_key] = (None, None)
                weather, temperature = weather_cache[cache_key]
                if weather:
                    weather_updated += 1
                else:
                    notes.append("weather_unavailable")
            else:
                weather_skipped += 1
                notes.append("weather_pending")

        upsert_match_context(
            conn,
            {
                "round": match["round_name"],
                "position": str(match["position"]),
                "venue": stadium[0] if stadium else "",
                "city": stadium[1] if stadium else "",
                "country": "England",
                "neutral_site": "0",
                "timezone": "Europe/London",
                "home_rest_days": "" if home_rest is None else str(home_rest),
                "away_rest_days": "" if away_rest is None else str(away_rest),
                "weather": weather or "",
                "temperature_c": "" if temperature is None else str(round(temperature, 1)),
                "notes": "; ".join(notes),
            },
        )
        contexts += 1

        for side, team_name, rest in [("home", home, home_rest), ("away", away, away_rest)]:
            team_id_row = conn.execute("SELECT id FROM teams WHERE lower(name) = lower(?)", (team_name,)).fetchone()
            if team_id_row is None:
                continue
            impact = _availability_impact(conn, team_name)
            confidence = round(max(0.45, 1.0 - impact * 0.7), 3)
            upsert_team_match_factor(
                conn,
                {
                    "round": match["round_name"],
                    "position": str(match["position"]),
                    "team": team_name,
                    "side": side,
                    "expected_lineup_confidence": str(confidence),
                    "absences_impact": str(impact),
                    "fatigue": "" if _fatigue(rest) is None else str(_fatigue(rest)),
                    "motivation": "0.5",
                    "notes": "auto_factors",
                },
            )
            factors += 1
    conn.commit()
    return contexts, factors, weather_checked, weather_updated, weather_skipped


def _latest_odds(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM match_odds
        WHERE match_id = ?
        ORDER BY captured_at DESC, bookmaker
        LIMIT 1
        """,
        (match_id,),
    ).fetchone()


def _team_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM teams WHERE lower(name) = lower(?)", (name,)).fetchone()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _odds_probabilities(row: sqlite3.Row | None) -> tuple[float, float, float] | None:
    if row is None:
        return None
    values = [_as_float(row["home_win"]), _as_float(row["draw"]), _as_float(row["away_win"])]
    if any(value is None or value <= 1.0 for value in values):
        return None
    raw = [1.0 / float(value) for value in values if value is not None]
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total) if total else None


def _model_probabilities(home_elo: float, away_elo: float) -> tuple[float, float, float]:
    elo_home_no_draw = 1.0 / (1.0 + math.pow(10.0, ((away_elo - home_elo - 65.0) / 400.0)))
    gap = abs((home_elo + 65.0) - away_elo)
    draw = _clamp(0.32 - min(gap, 300.0) / 300.0 * 0.11, 0.19, 0.33)
    home = (1.0 - draw) * elo_home_no_draw
    away = 1.0 - draw - home
    return home, draw, away


def _blend_probabilities(model: tuple[float, float, float], odds: tuple[float, float, float] | None) -> tuple[float, float, float]:
    if odds is None:
        return model
    blended = tuple(model[index] * 0.4 + odds[index] * 0.6 for index in range(3))
    total = sum(blended)
    return tuple(value / total for value in blended)  # type: ignore[return-value]


def _score_from_probabilities(home: float, draw: float, away: float, home_elo: float, away_elo: float) -> str:
    top = max(home, draw, away)
    elo_gap = (home_elo + 65.0) - away_elo
    if draw == top:
        return "1:1" if abs(elo_gap) < 110 else ("0:0" if top > 0.30 else "1:1")
    if home == top:
        if home >= 0.62:
            return "2:0" if away < 0.20 else "2:1"
        return "1:0" if draw >= 0.25 or away < 0.26 else "2:1"
    if away >= 0.58:
        return "0:2" if home < 0.22 else "1:2"
    return "0:1" if draw >= 0.25 or home < 0.28 else "1:2"


def sync_match_assessments(
    conn: sqlite3.Connection,
    updated_at: str,
    now: datetime | None = None,
    days_ahead: int = 365,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> int:
    current = _aware(now or datetime.now(ZoneInfo(timezone_name)), timezone_name)
    count = 0
    for match in _match_rows(conn):
        kickoff = _aware(parse_datetime(match["kickoff_at"]), timezone_name)
        if not _within_horizon(kickoff, current, days_ahead):
            continue
        home = _team_row(conn, str(match["home"]))
        away = _team_row(conn, str(match["away"]))
        home_elo = _as_float(home["elo_rating"] if home else None) or 1500.0
        away_elo = _as_float(away["elo_rating"] if away else None) or 1500.0
        model = _model_probabilities(home_elo, away_elo)
        probabilities = _blend_probabilities(model, _odds_probabilities(_latest_odds(conn, int(match["id"]))))
        home_prob, draw_prob, away_prob = probabilities
        top = max(probabilities)
        risk = "low" if top >= 0.55 else "medium" if top >= 0.44 else "high"
        volatility = round(1.0 - top + draw_prob * 0.3, 3)
        upsert_match_assessment(
            conn,
            {
                "round": match["round_name"],
                "position": str(match["position"]),
                "suggested_score": _score_from_probabilities(home_prob, draw_prob, away_prob, home_elo, away_elo),
                "risk_level": risk,
                "confidence": str(round(top, 3)),
                "home_edge": str(round(home_prob, 3)),
                "draw_edge": str(round(draw_prob, 3)),
                "away_edge": str(round(away_prob, 3)),
                "volatility": str(volatility),
                "consensus_note": "auto: Elo baseline blended with latest stored odds when present",
                "contrarian_note": "contest layer still needs field predictions before final pick",
                "notes": f"home_elo={round(home_elo, 1)}; away_elo={round(away_elo, 1)}",
                "updated_at": updated_at,
            },
        )
        count += 1
    conn.commit()
    return count


def sync_match_variables(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    days_ahead: int = 365,
    weather_days: int = 16,
    timeout: int = 30,
    timezone_name: str = DEFAULT_TIMEZONE,
    include_fpl: bool = True,
    include_elo: bool = True,
    include_context: bool = True,
    include_assessments: bool = True,
) -> VariableSyncResult:
    updated_at = _aware(now or datetime.now(timezone.utc), timezone_name).isoformat()
    errors: list[str] = []
    fpl_seen = fpl_imported = fpl_matched = 0
    fpl_unmatched: tuple[str, ...] = ()
    elo_checked = elo_updated = 0
    elo_unmatched: tuple[str, ...] = ()
    contexts = factors = weather_checked = weather_updated = weather_skipped = 0
    assessments = 0

    if include_fpl:
        try:
            fpl_seen, fpl_imported, fpl_matched, fpl_unmatched = sync_fpl_player_statuses(conn, updated_at, timeout=timeout)
        except (VariableSyncError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"FPL: {exc}")

    if include_elo:
        try:
            elo_checked, elo_updated, elo_unmatched = sync_clubelo_teams(conn, updated_at, timeout=timeout)
        except (VariableSyncError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            errors.append(f"ClubElo: {exc}")

    if include_context:
        try:
            contexts, factors, weather_checked, weather_updated, weather_skipped = sync_match_contexts_and_factors(
                conn,
                now=now,
                days_ahead=days_ahead,
                weather_days=weather_days,
                timeout=timeout,
                timezone_name=timezone_name,
            )
        except (VariableSyncError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"context/weather: {exc}")

    if include_assessments:
        try:
            assessments = sync_match_assessments(conn, updated_at, now=now, days_ahead=days_ahead, timezone_name=timezone_name)
        except (VariableSyncError, ValueError) as exc:
            errors.append(f"assessments: {exc}")

    return VariableSyncResult(
        updated_at=updated_at,
        fpl_players_seen=fpl_seen,
        fpl_players_imported=fpl_imported,
        fpl_teams_matched=fpl_matched,
        fpl_unmatched_teams=fpl_unmatched,
        elo_teams_checked=elo_checked,
        elo_teams_updated=elo_updated,
        elo_unmatched_teams=elo_unmatched,
        contexts_upserted=contexts,
        factors_upserted=factors,
        weather_checked=weather_checked,
        weather_updated=weather_updated,
        weather_skipped=weather_skipped,
        assessments_upserted=assessments,
        errors=tuple(errors),
    )
