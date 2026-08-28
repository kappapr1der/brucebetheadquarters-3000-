from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable

from .storage import upsert_team_form


FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
DEFAULT_COMPETITION = "PL"


class FootballDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class FootballDataFormResult:
    matches_seen: int = 0
    rows_upserted: int = 0
    teams_matched: int = 0
    fallback_teams: tuple[str, ...] = ()
    unmatched_teams: tuple[str, ...] = ()


class FootballDataClient:
    def __init__(self, token: str, base_url: str = FOOTBALL_DATA_BASE_URL, timeout: int = 30) -> None:
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if not self.token:
            raise FootballDataError("FOOTBALL_DATA_TOKEN is required")

    def _get(self, path: str, params: dict[str, str | int | None] | None = None) -> dict[str, object]:
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        request = urllib.request.Request(
            f"{self.base_url}{path}{suffix}",
            headers={
                "Accept": "application/json",
                "User-Agent": "BruceBetHQ/0.1",
                "X-Auth-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise FootballDataError(f"football-data.org HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise FootballDataError(f"football-data.org request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise FootballDataError("football-data.org returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FootballDataError("football-data.org returned an unexpected payload")
        return payload

    def competition_teams(self, competition: str = DEFAULT_COMPETITION) -> list[dict[str, object]]:
        payload = self._get(f"/competitions/{urllib.parse.quote(competition)}/teams")
        teams = payload.get("teams") or []
        if not isinstance(teams, list):
            raise FootballDataError("football-data.org teams payload is missing teams")
        return [item for item in teams if isinstance(item, dict)]

    def competition_matches(
        self,
        competition: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, object]]:
        payload = self._get(
            f"/competitions/{urllib.parse.quote(competition)}/matches",
            {"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED"},
        )
        matches = payload.get("matches") or []
        if not isinstance(matches, list):
            raise FootballDataError("football-data.org competition payload is missing matches")
        return [item for item in matches if isinstance(item, dict)]

    def team_matches(self, team_id: int, date_from: str, date_to: str, limit: int) -> list[dict[str, object]]:
        payload = self._get(
            f"/teams/{team_id}/matches",
            {"dateFrom": date_from, "dateTo": date_to, "status": "FINISHED", "limit": limit},
        )
        matches = payload.get("matches") or []
        if not isinstance(matches, list):
            raise FootballDataError("football-data.org team payload is missing matches")
        return [item for item in matches if isinstance(item, dict)]


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _match_date(match: dict[str, object]) -> str | None:
    raw = str(match.get("utcDate") or "").strip()
    if not raw:
        return None
    return raw[:10] if len(raw) >= 10 else None


def _match_sort_key(match: dict[str, object]) -> str:
    return str(match.get("utcDate") or "")


def _team_name(item: object) -> str:
    return str(item.get("name") or "") if isinstance(item, dict) else ""


def _team_id(item: object) -> int | None:
    return _as_int(item.get("id")) if isinstance(item, dict) else None


def _competition_name(match: dict[str, object], fallback: str) -> str:
    competition = match.get("competition")
    if isinstance(competition, dict):
        return str(competition.get("name") or competition.get("code") or fallback)
    return fallback


def _form_row(
    match: dict[str, object],
    local_team: str,
    resolve_team: Callable[[str], str | None],
    source_competition: str,
    expected_team_id: int | None = None,
) -> dict[str, str] | None:
    match_date = _match_date(match)
    score = match.get("score")
    full_time = score.get("fullTime") if isinstance(score, dict) else None
    if not match_date or not isinstance(full_time, dict):
        return None
    home = match.get("homeTeam")
    away = match.get("awayTeam")
    home_name = _team_name(home)
    away_name = _team_name(away)
    home_id = _team_id(home)
    away_id = _team_id(away)
    is_home = expected_team_id is not None and home_id == expected_team_id
    is_away = expected_team_id is not None and away_id == expected_team_id
    if not is_home and not is_away:
        is_home = resolve_team(home_name) == local_team
        is_away = resolve_team(away_name) == local_team
    if is_home == is_away:
        return None

    goals_for = _as_int(full_time.get("home" if is_home else "away"))
    goals_against = _as_int(full_time.get("away" if is_home else "home"))
    if goals_for is None or goals_against is None:
        return None
    opponent_external = away_name if is_home else home_name
    opponent = resolve_team(opponent_external) or opponent_external
    result = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
    match_id = str(match.get("id") or "unknown")
    return {
        "team": local_team,
        "match_date": match_date,
        "opponent": opponent,
        "venue": "home" if is_home else "away",
        "competition": _competition_name(match, source_competition),
        "goals_for": str(goals_for),
        "goals_against": str(goals_against),
        "result": result,
        "importance": "0.5",
        "notes": f"football-data.org match={match_id}; source={source_competition}",
    }


def _insert_recent_rows(
    conn: sqlite3.Connection,
    matches: Iterable[dict[str, object]],
    teams: Iterable[str],
    resolve_team: Callable[[str], str | None],
    source_competition: str,
    form_limit: int,
    team_ids: dict[str, int] | None = None,
) -> tuple[int, set[str]]:
    inserted = 0
    matched: set[str] = set()
    for team in teams:
        current = 0
        seen: set[tuple[str, str, str]] = set()
        for match in sorted(matches, key=_match_sort_key, reverse=True):
            row = _form_row(
                match,
                team,
                resolve_team,
                source_competition,
                expected_team_id=(team_ids or {}).get(team),
            )
            if row is None:
                continue
            key = (row["match_date"], row["opponent"], row["competition"])
            if key in seen:
                continue
            seen.add(key)
            upsert_team_form(conn, row)
            inserted += 1
            current += 1
            if current >= form_limit:
                matched.add(team)
                break
        if current:
            matched.add(team)
    return inserted, matched


def sync_recent_team_form(
    conn: sqlite3.Connection,
    token: str,
    active_teams: Iterable[str],
    resolve_team: Callable[[str], str | None],
    now: datetime | None = None,
    competition: str = DEFAULT_COMPETITION,
    form_limit: int = 5,
    lookback_days: int = 400,
    timeout: int = 30,
    client: FootballDataClient | None = None,
) -> FootballDataFormResult:
    """Store a small, source-tagged recent-form window for teams in the active contest."""

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    date_from = (current - timedelta(days=max(30, lookback_days))).date().isoformat()
    date_to = current.date().isoformat()
    requested_teams = tuple(dict.fromkeys(team.strip() for team in active_teams if team.strip()))
    if not requested_teams:
        return FootballDataFormResult()
    source = client or FootballDataClient(token, timeout=timeout)
    teams_payload = source.competition_teams(competition)
    team_ids: dict[str, int] = {}
    for item in teams_payload:
        local = resolve_team(_team_name(item))
        team_id = _team_id(item)
        if local in requested_teams and team_id is not None:
            team_ids[local] = team_id

    primary_matches = source.competition_matches(competition, date_from, date_to)
    inserted, matched = _insert_recent_rows(
        conn,
        primary_matches,
        requested_teams,
        resolve_team,
        competition,
        form_limit,
        team_ids=team_ids,
    )
    fallback_teams: list[str] = []
    for team in requested_teams:
        if team in matched and _team_form_count(conn, team) >= form_limit:
            continue
        team_id = team_ids.get(team)
        if team_id is None:
            continue
        fallback_teams.append(team)
        team_matches = source.team_matches(team_id, date_from, date_to, max(10, form_limit * 2))
        fallback_inserted, fallback_matched = _insert_recent_rows(
            conn,
            team_matches,
            [team],
            resolve_team,
            "all-competitions",
            form_limit,
            team_ids={team: team_id},
        )
        inserted += fallback_inserted
        matched.update(fallback_matched)
    conn.commit()
    unmatched = tuple(sorted(team for team in requested_teams if team not in matched))
    return FootballDataFormResult(
        matches_seen=len(primary_matches),
        rows_upserted=inserted,
        teams_matched=len(matched),
        fallback_teams=tuple(fallback_teams),
        unmatched_teams=unmatched,
    )


def _team_form_count(conn: sqlite3.Connection, team: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM team_form tf
        JOIN teams t ON t.id = tf.team_id
        WHERE lower(t.name) = lower(?)
        """,
        (team,),
    ).fetchone()
    return int(row["count"] or 0) if row else 0
