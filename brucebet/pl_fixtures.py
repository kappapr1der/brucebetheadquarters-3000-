from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo
import sqlite3
import urllib.parse
import urllib.request

from .storage import FixtureIdentityError, active_season_id, upsert_match


PL_API_BASE = "https://footballapi.pulselive.com/football"
DEFAULT_PL_COMPSEASON_ID = 841
DEFAULT_PL_SEASON_LABEL = "2026/2027"
DEFAULT_TIMEZONE = "Europe/Moscow"
PL_FIXTURE_SOURCE = "premierleague.com"
TEMP_POSITION_OFFSET = 1_000_000_000


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
    created: int = 0
    updated: int = 0
    moved: int = 0
    unmatched: int = 0
    stale_factors_removed: int = 0
    before_hash: str = ""
    after_hash: str = ""


@dataclass(frozen=True)
class PreparedFixture:
    source_fixture_id: str
    round_number: int
    position: int
    home: str
    away: str
    kickoff_at: str | None
    result: str | None


@dataclass(frozen=True)
class ResultSyncResult:
    source: str
    fetched: int
    finished_seen: int
    matched: int
    updated: int
    unmatched: tuple[str, ...]


class PremierLeagueApiError(RuntimeError):
    pass


class FixtureSyncError(PremierLeagueApiError):
    def __init__(self, message: str, *, unmatched: int = 1) -> None:
        super().__init__(message)
        self.unmatched = max(1, int(unmatched))


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
    home = score.get("homeScore") if score.get("homeScore") is not None else score.get("home")
    away = score.get("awayScore") if score.get("awayScore") is not None else score.get("away")
    try:
        return f"{int(home)}:{int(away)}" if home is not None and away is not None else None
    except (TypeError, ValueError):
        return None


def fixture_is_finished(fixture: dict[str, object]) -> bool:
    """Return true only for fixtures the official feed marks as complete."""
    status = str(fixture.get("status") or "").strip().upper()
    return status in {"C", "COMPLETED", "FINISHED", "FT", "AET", "PEN"}


def _team_key(name: str) -> str:
    value = name.lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", value)


def fixture_source_id(fixture: dict[str, object]) -> str:
    raw = fixture.get("id")
    if raw is None or isinstance(raw, bool):
        raise FixtureSyncError("Premier League fixture is missing a stable id")
    try:
        numeric = float(raw)
        if numeric.is_integer():
            return str(int(numeric))
    except (TypeError, ValueError):
        pass
    value = str(raw).strip()
    if not value:
        raise FixtureSyncError("Premier League fixture has a blank stable id")
    return value


def _prepare_fixtures(
    fixtures: list[dict[str, object]],
    *,
    timezone_name: str,
) -> list[PreparedFixture]:
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for fixture in fixtures:
        round_number = matchday(fixture)
        if round_number <= 0:
            raise FixtureSyncError("Premier League fixture is missing a valid matchday")
        grouped[round_number].append(fixture)

    prepared: list[PreparedFixture] = []
    seen_source_ids: set[str] = set()
    seen_team_pairs: set[tuple[str, str]] = set()
    for round_number in sorted(grouped):
        round_fixtures = sorted(
            grouped[round_number],
            key=lambda item: (
                float(((item.get("kickoff") or {}) or {}).get("millis") or 0),
                float(item.get("id") or 0),
            ),
        )
        for position, fixture in enumerate(round_fixtures, start=1):
            source_id = fixture_source_id(fixture)
            if source_id in seen_source_ids:
                raise FixtureSyncError(f"Duplicate Premier League fixture id: {source_id}")
            teams = fixture.get("teams") or []
            if not isinstance(teams, list) or len(teams) < 2:
                raise FixtureSyncError(f"Fixture {source_id} has no home/away teams")
            home = team_name(teams[0])
            away = team_name(teams[1])
            if not home or not away:
                raise FixtureSyncError(f"Fixture {source_id} has a blank home/away team")
            pair = (_team_key(home), _team_key(away))
            if pair in seen_team_pairs:
                raise FixtureSyncError(f"Ambiguous duplicate team pair in PL feed: {home} - {away}")
            seen_source_ids.add(source_id)
            seen_team_pairs.add(pair)
            prepared.append(
                PreparedFixture(
                    source_fixture_id=source_id,
                    round_number=round_number,
                    position=position,
                    home=home,
                    away=away,
                    kickoff_at=kickoff_iso(fixture, timezone_name=timezone_name),
                    result=result_score(fixture) if fixture_is_finished(fixture) else None,
                )
            )
    return prepared


def _season_fixture_rows(conn: sqlite3.Connection, season_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                m.id, m.round_id, r.name AS round_name, m.position,
                m.home, m.away, m.kickoff_at, m.result,
                m.source, m.source_fixture_id
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            ORDER BY m.id
            """,
            (season_id,),
        )
    )


def _fixture_state_hash(conn: sqlite3.Connection, season_id: int) -> str:
    rows = [dict(row) for row in _season_fixture_rows(conn, season_id)]
    payload = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _backfill_fixture_identities(
    conn: sqlite3.Connection,
    season_id: int,
    prepared: list[PreparedFixture],
    *,
    changed_at: str,
) -> set[int]:
    existing = _season_fixture_rows(conn, season_id)
    if not existing:
        return set()

    unkeyed = [
        row
        for row in existing
        if not str(row["source"] or "").strip() or not str(row["source_fixture_id"] or "").strip()
    ]
    if not unkeyed:
        return set()
    if len(unkeyed) != len(existing):
        raise FixtureSyncError("Fixture identity migration found a partially keyed active season")
    if len(existing) != len(prepared):
        raise FixtureSyncError(
            "Fixture identity migration requires complete one-to-one coverage: "
            f"database={len(existing)}, source={len(prepared)}",
            unmatched=abs(len(existing) - len(prepared)) or 1,
        )

    source_by_pair: dict[tuple[str, str], PreparedFixture] = {}
    for fixture in prepared:
        pair = (_team_key(fixture.home), _team_key(fixture.away))
        if pair in source_by_pair:
            raise FixtureSyncError(f"Ambiguous source pair: {fixture.home} - {fixture.away}")
        source_by_pair[pair] = fixture

    database_by_pair: dict[tuple[str, str], sqlite3.Row] = {}
    for row in existing:
        pair = (_team_key(str(row["home"])), _team_key(str(row["away"])))
        if pair in database_by_pair:
            raise FixtureSyncError(
                f"Ambiguous database pair: {row['home']} - {row['away']}",
                unmatched=2,
            )
        database_by_pair[pair] = row

    missing_in_source = sorted(set(database_by_pair) - set(source_by_pair))
    missing_in_database = sorted(set(source_by_pair) - set(database_by_pair))
    if missing_in_source or missing_in_database:
        raise FixtureSyncError(
            "Fixture identity migration did not find 1:1 season+home+away matches: "
            f"database_only={len(missing_in_source)}, source_only={len(missing_in_database)}",
            unmatched=len(missing_in_source) + len(missing_in_database),
        )

    backfilled: set[int] = set()
    for pair, row in database_by_pair.items():
        fixture = source_by_pair[pair]
        match_id = int(row["id"])
        conn.execute(
            "UPDATE matches SET source = ?, source_fixture_id = ? WHERE id = ?",
            (PL_FIXTURE_SOURCE, fixture.source_fixture_id, match_id),
        )
        conn.execute(
            """
            INSERT INTO fixture_identity_events(
                match_id, event_type, source, source_fixture_id,
                old_home, old_away, new_home, new_away,
                old_round_id, new_round_id, old_position, new_position,
                created_at, details
            )
            VALUES(?, 'source_identity_backfilled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                PL_FIXTURE_SOURCE,
                fixture.source_fixture_id,
                row["home"],
                row["away"],
                row["home"],
                row["away"],
                int(row["round_id"]),
                int(row["round_id"]),
                int(row["position"]),
                int(row["position"]),
                changed_at,
                "Matched active season by normalized home and away teams",
            ),
        )
        backfilled.add(match_id)
    return backfilled


def _remove_stale_team_match_factors(conn: sqlite3.Connection, season_id: int) -> int:
    cursor = conn.execute(
        """
        DELETE FROM team_match_factors AS factor
        WHERE EXISTS (
            SELECT 1
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE m.id = factor.match_id AND r.season_id = ?
        )
        AND NOT EXISTS (
            SELECT 1
            FROM matches m
            JOIN teams t ON t.name = m.home OR t.name = m.away
            WHERE m.id = factor.match_id AND t.id = factor.team_id
        )
        """,
        (season_id,),
    )
    conn.execute(
        """
        UPDATE team_match_factors AS factor
        SET side = (
            SELECT CASE WHEN t.name = m.home THEN 'home' ELSE 'away' END
            FROM matches m
            JOIN teams t ON t.id = factor.team_id
            WHERE m.id = factor.match_id
        )
        WHERE EXISTS (
            SELECT 1
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            JOIN teams t ON t.id = factor.team_id
            WHERE m.id = factor.match_id
              AND r.season_id = ?
              AND (t.name = m.home OR t.name = m.away)
        )
        """,
        (season_id,),
    )
    return max(0, int(cursor.rowcount))


def _record_fixture_sync_run(
    conn: sqlite3.Connection,
    *,
    started_at: str,
    finished_at: str,
    source_item_count: int,
    created: int,
    updated: int,
    moved: int,
    unmatched: int,
    stale_factors_removed: int,
    before_hash: str,
    after_hash: str,
    status: str,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fixture_sync_runs(
            source, started_at, finished_at, source_item_count,
            created, updated, moved, unmatched, stale_factors_removed,
            before_hash, after_hash, status, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PL_FIXTURE_SOURCE,
            started_at,
            finished_at,
            source_item_count,
            created,
            updated,
            moved,
            unmatched,
            stale_factors_removed,
            before_hash,
            after_hash,
            status,
            notes,
        ),
    )


def import_pl_results(conn: sqlite3.Connection, fixtures: list[dict[str, object]]) -> ResultSyncResult:
    """Import completed results without altering the calendar or incomplete matches."""
    season_id = active_season_id(conn)
    db_matches = list(
        conn.execute(
            """
            SELECT m.id, m.home, m.away
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            """,
            (season_id,),
        )
    )
    by_teams = {(_team_key(row["home"]), _team_key(row["away"])): int(row["id"]) for row in db_matches}
    finished_seen = 0
    matched = 0
    updated = 0
    unmatched: list[str] = []

    for fixture in fixtures:
        if not fixture_is_finished(fixture):
            continue
        finished_seen += 1
        teams = fixture.get("teams") or []
        if not isinstance(teams, list) or len(teams) < 2:
            continue
        home = team_name(teams[0])
        away = team_name(teams[1])
        score = result_score(fixture)
        match_id = by_teams.get((_team_key(home), _team_key(away)))
        if not home or not away or not score or match_id is None:
            unmatched.append(f"{home or '?'} - {away or '?'}")
            continue
        matched += 1
        cursor = conn.execute(
            "UPDATE matches SET result = ? WHERE id = ? AND COALESCE(result, '') <> ?",
            (score, match_id, score),
        )
        updated += int(cursor.rowcount)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO result_sync_runs(
            source, started_at, finished_at, fixtures_seen, finished_seen, matched, updated, unmatched, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "premierleague.com public API",
            now,
            now,
            len(fixtures),
            finished_seen,
            matched,
            updated,
            len(unmatched),
            "; ".join(unmatched[:20]),
        ),
    )
    conn.commit()
    return ResultSyncResult(
        source="premierleague.com public API",
        fetched=len(fixtures),
        finished_seen=finished_seen,
        matched=matched,
        updated=updated,
        unmatched=tuple(unmatched),
    )


def import_pl_fixtures(
    conn: sqlite3.Connection,
    fixtures: list[dict[str, object]],
    compseason_id: int = DEFAULT_PL_COMPSEASON_ID,
    season_label: str = DEFAULT_PL_SEASON_LABEL,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> FixtureSyncResult:
    fixture_items = list(fixtures)
    season_id = active_season_id(conn)
    started_at = datetime.now(timezone.utc).isoformat()
    before_hash = _fixture_state_hash(conn, season_id)
    conn.execute("SAVEPOINT fixture_sync")
    try:
        prepared = _prepare_fixtures(fixture_items, timezone_name=timezone_name)
        backfilled_ids = _backfill_fixture_identities(
            conn,
            season_id,
            prepared,
            changed_at=started_at,
        )
        existing = _season_fixture_rows(conn, season_id)
        foreign_sources = [
            row
            for row in existing
            if str(row["source"] or "").strip() != PL_FIXTURE_SOURCE
            or not str(row["source_fixture_id"] or "").strip()
        ]
        if foreign_sources:
            raise FixtureSyncError(
                "Active season contains matches without the Premier League stable identity",
                unmatched=len(foreign_sources),
            )

        incoming_ids = {item.source_fixture_id for item in prepared}
        existing_ids = {str(row["source_fixture_id"]) for row in existing}
        missing_from_feed = sorted(existing_ids - incoming_ids)
        if missing_from_feed:
            raise FixtureSyncError(
                "Premier League feed is incomplete; existing fixture ids are missing: "
                + ", ".join(missing_from_feed[:10]),
                unmatched=len(missing_from_feed),
            )

        old_by_source_id = {str(row["source_fixture_id"]): row for row in existing}
        if existing:
            conn.execute(
                """
                UPDATE matches
                SET position = ? - id
                WHERE id IN (
                    SELECT m.id
                    FROM matches m
                    JOIN rounds r ON r.id = m.round_id
                    WHERE r.season_id = ?
                )
                """,
                (-TEMP_POSITION_OFFSET, season_id),
            )

        created_ids: set[int] = set()
        updated_ids: set[int] = set(backfilled_ids)
        moved_ids: set[int] = set()
        kickoffs: list[str] = []
        for item in prepared:
            previous = old_by_source_id.get(item.source_fixture_id)
            if previous is not None:
                if str(previous["round_name"]) != str(item.round_number) or int(previous["position"]) != item.position:
                    moved_ids.add(int(previous["id"]))
                if (
                    previous["kickoff_at"] != item.kickoff_at
                    or (item.result is not None and previous["result"] != item.result)
                ):
                    updated_ids.add(int(previous["id"]))
            try:
                match_id = upsert_match(
                    conn,
                    round_name=str(item.round_number),
                    position=item.position,
                    home=item.home,
                    away=item.away,
                    kickoff_at=item.kickoff_at,
                    result=item.result,
                    source=PL_FIXTURE_SOURCE,
                    source_fixture_id=item.source_fixture_id,
                )
            except FixtureIdentityError as exc:
                raise FixtureSyncError(str(exc)) from exc
            if previous is None:
                created_ids.add(match_id)
            if item.kickoff_at:
                kickoffs.append(item.kickoff_at)

        mapped_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND m.source = ? AND m.source_fixture_id IS NOT NULL
            """,
            (season_id, PL_FIXTURE_SOURCE),
        ).fetchone()[0]
        if int(mapped_count) != len(prepared):
            raise FixtureSyncError(
                f"Fixture sync mapped {mapped_count}/{len(prepared)} source ids",
                unmatched=abs(int(mapped_count) - len(prepared)) or 1,
            )

        stale_factors_removed = _remove_stale_team_match_factors(conn, season_id)
        after_hash = _fixture_state_hash(conn, season_id)
        finished_at = datetime.now(timezone.utc).isoformat()
        _record_fixture_sync_run(
            conn,
            started_at=started_at,
            finished_at=finished_at,
            source_item_count=len(fixture_items),
            created=len(created_ids),
            updated=len(updated_ids),
            moved=len(moved_ids),
            unmatched=0,
            stale_factors_removed=stale_factors_removed,
            before_hash=before_hash,
            after_hash=after_hash,
            status="success",
        )
        conn.execute("RELEASE SAVEPOINT fixture_sync")
        conn.commit()
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT fixture_sync")
        conn.execute("RELEASE SAVEPOINT fixture_sync")
        after_hash = _fixture_state_hash(conn, season_id)
        unmatched = exc.unmatched if isinstance(exc, FixtureSyncError) else 1
        _record_fixture_sync_run(
            conn,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            source_item_count=len(fixture_items),
            created=0,
            updated=0,
            moved=0,
            unmatched=unmatched,
            stale_factors_removed=0,
            before_hash=before_hash,
            after_hash=after_hash,
            status="failed",
            notes=str(exc)[:1000],
        )
        conn.commit()
        if isinstance(exc, FixtureSyncError):
            raise
        raise FixtureSyncError(f"Fixture sync failed: {exc}") from exc

    return FixtureSyncResult(
        source="premierleague.com public API",
        compseason_id=compseason_id,
        season_label=season_label,
        fetched=len(fixture_items),
        imported=len(prepared),
        rounds=len({item.round_number for item in prepared}),
        first_kickoff=min(kickoffs) if kickoffs else None,
        last_kickoff=max(kickoffs) if kickoffs else None,
        created=len(created_ids),
        updated=len(updated_ids),
        moved=len(moved_ids),
        unmatched=0,
        stale_factors_removed=stale_factors_removed,
        before_hash=before_hash,
        after_hash=after_hash,
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


def sync_pl_results_to_db(
    conn: sqlite3.Connection,
    compseason_id: int | None = DEFAULT_PL_COMPSEASON_ID,
    season_label: str = DEFAULT_PL_SEASON_LABEL,
) -> ResultSyncResult:
    client = PremierLeaguePublicClient()
    resolved_id = compseason_id or client.resolve_compseason_id(season_label)
    return import_pl_results(conn, client.fixtures(resolved_id))
