from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from zoneinfo import ZoneInfo
import sqlite3
import urllib.parse
import urllib.request

from .storage import upsert_match


PL_API_BASE = "https://footballapi.pulselive.com/football"
DEFAULT_PL_COMPSEASON_ID = 841
DEFAULT_PL_SEASON_LABEL = "2026/2027"
DEFAULT_TIMEZONE = "Europe/Moscow"


@dataclass(frozen=True)
class FixtureSyncResult:
    source: str
    compseason_id: int
    season_label: str
    fetched: int
    imported: int
    rounds: int
    first_kickoff: str | None
    last_kickoff: str | None


class PremierLeagueApiError(RuntimeError):
    pass


class PremierLeaguePublicClient:
    def __init__(self, base_url: str = PL_API_BASE, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, str | int | bool]) -> dict[str, object]:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Origin": "https://www.premierleague.com",
                "User-Agent": "Mozilla/5.0 BruceBetHQ/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - keep API failures compact for bot/CLI users.
            raise PremierLeagueApiError(f"Premier League public API failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise PremierLeagueApiError("Premier League public API returned unexpected payload")
        return payload

    def compseasons(self) -> list[dict[str, object]]:
        payload = self._get("/competitions/1/compseasons", {"page": 0, "pageSize": 100, "comps": 1})
        content = payload.get("content") or []
        return [item for item in content if isinstance(item, dict)]

    def resolve_compseason_id(self, season_label: str = DEFAULT_PL_SEASON_LABEL) -> int:
        normalized = season_label.replace("-", "/")
        for item in self.compseasons():
            label = str(item.get("label") or "")
            if normalized in label or label.endswith(normalized):
                return int(float(item["id"]))
        raise PremierLeagueApiError(f"Premier League compSeason not found for {season_label}")

    def fixtures(self, compseason_id: int, page_size: int = 100) -> list[dict[str, object]]:
        page = 0
        fixtures: list[dict[str, object]] = []
        while True:
            payload = self._get(
                "/fixtures",
                {
                    "comps": 1,
                    "compSeasons": compseason_id,
                    "page": page,
                    "pageSize": page_size,
                    "sort": "asc",
                    "altIds": "true",
                },
            )
            fixtures.extend(item for item in (payload.get("content") or []) if isinstance(item, dict))
            page_info = payload.get("pageInfo") or {}
            num_pages = int(page_info.get("numPages") or 0)
            page += 1
            if page >= num_pages:
                break
        return fixtures


def kickoff_iso(fixture: dict[str, object], timezone_name: str = DEFAULT_TIMEZONE) -> str | None:
    kickoff = fixture.get("kickoff") or {}
    if not isinstance(kickoff, dict) or kickoff.get("millis") is None:
        return None
    millis = float(kickoff["millis"])
    dt = datetime.fromtimestamp(millis / 1000, timezone.utc).astimezone(ZoneInfo(timezone_name))
    return dt.isoformat()


def team_name(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    team = entry.get("team") or {}
    if not isinstance(team, dict):
        return ""
    club = team.get("club") or {}
    if isinstance(club, dict) and club.get("name"):
        return str(club["name"]).strip()
    return str(team.get("name") or "").strip()


def matchday(fixture: dict[str, object]) -> int:
    gameweek = fixture.get("gameweek") or {}
    if not isinstance(gameweek, dict):
        return 0
    return int(float(gameweek.get("gameweek") or 0))


def result_score(fixture: dict[str, object]) -> str | None:
    score = fixture.get("score") or {}
    if not isinstance(score, dict):
        return None
    home = score.get("homeScore") or score.get("home")
    away = score.get("awayScore") or score.get("away")
    try:
        return f"{int(home)}:{int(away)}" if home is not None and away is not None else None
    except (TypeError, ValueError):
        return None


def import_pl_fixtures(
    conn: sqlite3.Connection,
    fixtures: list[dict[str, object]],
    compseason_id: int = DEFAULT_PL_COMPSEASON_ID,
    season_label: str = DEFAULT_PL_SEASON_LABEL,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> FixtureSyncResult:
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for fixture in fixtures:
        round_number = matchday(fixture)
        if round_number:
            grouped[round_number].append(fixture)

    imported = 0
    kickoffs: list[str] = []
    for round_number in sorted(grouped):
        round_fixtures = sorted(
            grouped[round_number],
            key=lambda item: (
                float(((item.get("kickoff") or {}) or {}).get("millis") or 0),
                float(item.get("id") or 0),
            ),
        )
        for position, fixture in enumerate(round_fixtures, start=1):
            teams = fixture.get("teams") or []
            if not isinstance(teams, list) or len(teams) < 2:
                continue
            home = team_name(teams[0])
            away = team_name(teams[1])
            kickoff_at = kickoff_iso(fixture, timezone_name=timezone_name)
            if not home or not away:
                continue
            upsert_match(
                conn,
                round_name=str(round_number),
                position=position,
                home=home,
                away=away,
                kickoff_at=kickoff_at,
                result=result_score(fixture),
            )
            imported += 1
            if kickoff_at:
                kickoffs.append(kickoff_at)
    conn.commit()
    return FixtureSyncResult(
        source="premierleague.com public API",
        compseason_id=compseason_id,
        season_label=season_label,
        fetched=len(fixtures),
        imported=imported,
        rounds=len(grouped),
        first_kickoff=min(kickoffs) if kickoffs else None,
        last_kickoff=max(kickoffs) if kickoffs else None,
    )


def sync_pl_fixtures_to_db(
    conn: sqlite3.Connection,
    compseason_id: int | None = DEFAULT_PL_COMPSEASON_ID,
    season_label: str = DEFAULT_PL_SEASON_LABEL,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> FixtureSyncResult:
    client = PremierLeaguePublicClient()
    resolved_id = compseason_id or client.resolve_compseason_id(season_label)
    fixtures = client.fixtures(resolved_id)
    return import_pl_fixtures(
        conn,
        fixtures,
        compseason_id=resolved_id,
        season_label=season_label,
        timezone_name=timezone_name,
    )
