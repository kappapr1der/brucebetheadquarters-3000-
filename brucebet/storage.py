from __future__ import annotations

import csv
from pathlib import Path
import sqlite3

from .scoring import parse_datetime, parse_score


DEFAULT_COMPETITION_CODE = "epl"
DEFAULT_COMPETITION_NAME = "English Premier League"
DEFAULT_SEASON_NAME = "2026/27"
LEGACY_COMPETITION_CODE = "legacy"
LEGACY_COMPETITION_NAME = "Legacy imported data"
LEGACY_SEASON_NAME = "pre-season-model"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS competitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    display_name TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    entry_fee_rub INTEGER NOT NULL DEFAULT 300,
    payout_first REAL NOT NULL DEFAULT 0.5,
    payout_second REAL NOT NULL DEFAULT 0.3,
    payout_third REAL NOT NULL DEFAULT 0.2,
    deadline_lock_minutes INTEGER NOT NULL DEFAULT 90,
    notes TEXT,
    UNIQUE(competition_id, name)
);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    paid INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS season_participants (
    season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    paid INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    alias TEXT,
    notes TEXT,
    PRIMARY KEY(season_id, participant_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    short_name TEXT,
    country TEXT,
    confederation TEXT,
    fifa_rank INTEGER,
    elo_rating REAL,
    market_value_m_eur REAL,
    manager TEXT,
    preferred_formation TEXT,
    attack_rating REAL,
    defense_rating REAL,
    transition_rating REAL,
    set_piece_rating REAL,
    goalkeeper_rating REAL,
    style_tags TEXT,
    notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_id INTEGER REFERENCES seasons(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    deadline_at TEXT,
    UNIQUE(season_id, name)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    kickoff_at TEXT,
    result TEXT,
    UNIQUE(round_id, position)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    score TEXT NOT NULL,
    submitted_at TEXT,
    source TEXT,
    UNIQUE(participant_id, match_id)
);

CREATE TABLE IF NOT EXISTS team_form (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    match_date TEXT NOT NULL,
    opponent TEXT NOT NULL,
    venue TEXT,
    competition TEXT,
    goals_for INTEGER,
    goals_against INTEGER,
    xg_for REAL,
    xg_against REAL,
    result TEXT,
    importance REAL,
    notes TEXT,
    UNIQUE(team_id, match_date, opponent, competition)
);

CREATE TABLE IF NOT EXISTS absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player TEXT NOT NULL,
    role TEXT,
    status TEXT NOT NULL,
    severity TEXT,
    impact_rating REAL,
    expected_return TEXT,
    source TEXT,
    notes TEXT,
    updated_at TEXT,
    UNIQUE(team_id, player, status)
);

CREATE TABLE IF NOT EXISTS match_contexts (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    venue TEXT,
    city TEXT,
    country TEXT,
    neutral_site INTEGER,
    timezone TEXT,
    home_rest_days INTEGER,
    away_rest_days INTEGER,
    home_travel_km REAL,
    away_travel_km REAL,
    weather TEXT,
    temperature_c REAL,
    pitch TEXT,
    referee TEXT,
    home_motivation REAL,
    away_motivation REAL,
    home_rotation_risk REAL,
    away_rotation_risk REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS match_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    bookmaker TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    home_win REAL,
    draw REAL,
    away_win REAL,
    over_2_5 REAL,
    under_2_5 REAL,
    btts_yes REAL,
    btts_no REAL,
    notes TEXT,
    UNIQUE(match_id, bookmaker, captured_at)
);

CREATE TABLE IF NOT EXISTS team_match_factors (
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    side TEXT NOT NULL,
    expected_lineup_confidence REAL,
    absences_impact REAL,
    fatigue REAL,
    morale REAL,
    tactical_fit REAL,
    pressing_advantage REAL,
    set_piece_edge REAL,
    motivation REAL,
    notes TEXT,
    PRIMARY KEY(match_id, team_id)
);

CREATE TABLE IF NOT EXISTS match_assessments (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    suggested_score TEXT,
    risk_level TEXT,
    confidence REAL,
    home_edge REAL,
    away_edge REAL,
    draw_edge REAL,
    volatility REAL,
    consensus_note TEXT,
    contrarian_note TEXT,
    notes TEXT,
    updated_at TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate_db(conn)
    activate_profile(conn)
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        DROP TABLE IF EXISTS match_assessments;
        DROP TABLE IF EXISTS team_match_factors;
        DROP TABLE IF EXISTS match_odds;
        DROP TABLE IF EXISTS match_contexts;
        DROP TABLE IF EXISTS absences;
        DROP TABLE IF EXISTS team_form;
        DROP TABLE IF EXISTS predictions;
        DROP TABLE IF EXISTS matches;
        DROP TABLE IF EXISTS rounds;
        DROP TABLE IF EXISTS teams;
        DROP TABLE IF EXISTS season_participants;
        DROP TABLE IF EXISTS participants;
        DROP TABLE IF EXISTS seasons;
        DROP TABLE IF EXISTS competitions;
        PRAGMA foreign_keys = ON;
        """
    )
    init_db(conn)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def unique_index_columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    indexes: list[tuple[str, ...]] = []
    for row in conn.execute(f"PRAGMA index_list({table})"):
        if not int(row["unique"]):
            continue
        index_name = row["name"]
        columns = tuple(index_row["name"] for index_row in conn.execute(f"PRAGMA index_info({index_name})"))
        indexes.append(columns)
    return indexes


def ensure_legacy_season(conn: sqlite3.Connection) -> int:
    competition_id = ensure_competition(conn, LEGACY_COMPETITION_CODE, LEGACY_COMPETITION_NAME)
    conn.execute(
        """
        INSERT INTO seasons(competition_id, name, display_name, active)
        VALUES(?, ?, ?, 0)
        ON CONFLICT(competition_id, name) DO UPDATE SET display_name = excluded.display_name
        """,
        (competition_id, LEGACY_SEASON_NAME, "Legacy pre-season data"),
    )
    row = conn.execute(
        "SELECT id FROM seasons WHERE competition_id = ? AND name = ?",
        (competition_id, LEGACY_SEASON_NAME),
    ).fetchone()
    return int(row["id"])


def rebuild_rounds_for_seasons(conn: sqlite3.Connection, fallback_season_id: int) -> None:
    old_columns = table_columns(conn, "rounds")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS rounds_new")
    conn.execute(
        """
        CREATE TABLE rounds_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER REFERENCES seasons(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            deadline_at TEXT,
            UNIQUE(season_id, name)
        )
        """
    )
    if "season_id" in old_columns:
        conn.execute(
            """
            INSERT INTO rounds_new(id, season_id, name, sort_order, deadline_at)
            SELECT id, COALESCE(season_id, ?), name, sort_order, deadline_at
            FROM rounds
            """,
            (fallback_season_id,),
        )
    else:
        conn.execute(
            """
            INSERT INTO rounds_new(id, season_id, name, sort_order, deadline_at)
            SELECT id, ?, name, sort_order, deadline_at
            FROM rounds
            """,
            (fallback_season_id,),
        )
    conn.execute("DROP TABLE rounds")
    conn.execute("ALTER TABLE rounds_new RENAME TO rounds")
    conn.execute("PRAGMA foreign_keys = ON")


def migrate_db(conn: sqlite3.Connection) -> None:
    rounds_columns = table_columns(conn, "rounds")
    unique_indexes = unique_index_columns(conn, "rounds")
    has_legacy_round_name_unique = ("name",) in unique_indexes
    if "season_id" not in rounds_columns or has_legacy_round_name_unique:
        rebuild_rounds_for_seasons(conn, ensure_legacy_season(conn))


def ensure_competition(conn: sqlite3.Connection, code: str, name: str | None = None) -> int:
    normalized = code.strip().lower()
    display_name = name.strip() if name else normalized.upper()
    conn.execute(
        """
        INSERT INTO competitions(code, name)
        VALUES(?, ?)
        ON CONFLICT(code) DO UPDATE SET name = excluded.name
        """,
        (normalized, display_name),
    )
    row = conn.execute("SELECT id FROM competitions WHERE code = ?", (normalized,)).fetchone()
    return int(row["id"])


def activate_profile(
    conn: sqlite3.Connection,
    competition_code: str = DEFAULT_COMPETITION_CODE,
    season_name: str = DEFAULT_SEASON_NAME,
    competition_name: str | None = DEFAULT_COMPETITION_NAME,
    season_display_name: str | None = None,
    entry_fee_rub: int = 300,
    lock_minutes: int = 90,
) -> int:
    competition_id = ensure_competition(conn, competition_code, competition_name)
    display = season_display_name or f"{competition_code.upper()} {season_name}"
    conn.execute("UPDATE seasons SET active = 0")
    conn.execute(
        """
        INSERT INTO seasons(
            competition_id, name, display_name, active, entry_fee_rub, deadline_lock_minutes
        )
        VALUES(?, ?, ?, 1, ?, ?)
        ON CONFLICT(competition_id, name) DO UPDATE SET
            display_name = excluded.display_name,
            active = 1,
            entry_fee_rub = excluded.entry_fee_rub,
            deadline_lock_minutes = excluded.deadline_lock_minutes
        """,
        (competition_id, season_name.strip(), display, entry_fee_rub, lock_minutes),
    )
    row = conn.execute(
        "SELECT id FROM seasons WHERE competition_id = ? AND name = ?",
        (competition_id, season_name.strip()),
    ).fetchone()
    season_id = int(row["id"])
    if "season_id" in table_columns(conn, "rounds"):
        conn.execute("UPDATE rounds SET season_id = ? WHERE season_id IS NULL", (season_id,))
    conn.commit()
    return season_id


def active_season(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT s.*, c.code AS competition_code, c.name AS competition_name
        FROM seasons s
        JOIN competitions c ON c.id = s.competition_id
        WHERE s.active = 1
        ORDER BY s.id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        activate_profile(conn)
        row = conn.execute(
            """
            SELECT s.*, c.code AS competition_code, c.name AS competition_name
            FROM seasons s
            JOIN competitions c ON c.id = s.competition_id
            WHERE s.active = 1
            ORDER BY s.id DESC
            LIMIT 1
            """
        ).fetchone()
    return row


def active_season_id(conn: sqlite3.Connection) -> int:
    return int(active_season(conn)["id"])


def ensure_season_participant(
    conn: sqlite3.Connection,
    participant_id: int,
    paid: int | None = None,
    active: int = 1,
) -> None:
    season_id = active_season_id(conn)
    existing = conn.execute(
        "SELECT paid FROM season_participants WHERE season_id = ? AND participant_id = ?",
        (season_id, participant_id),
    ).fetchone()
    paid_value = paid if paid is not None else (int(existing["paid"]) if existing else 1)
    conn.execute(
        """
        INSERT INTO season_participants(season_id, participant_id, paid, active)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(season_id, participant_id) DO UPDATE SET
            paid = excluded.paid,
            active = excluded.active
        """,
        (season_id, participant_id, paid_value, active),
    )


def truthy(raw: str | None) -> int:
    if raw is None:
        return 1
    return 0 if raw.strip().lower() in {"0", "false", "no", "нет", "не"} else 1


def optional_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def optional_int(raw: str | None) -> int | None:
    value = optional_text(raw)
    return int(value) if value is not None else None


def optional_float(raw: str | None) -> float | None:
    value = optional_text(raw)
    return float(value) if value is not None else None


def optional_iso_datetime(raw: str | None) -> str | None:
    value = optional_text(raw)
    return parse_datetime(value).isoformat() if value is not None else None


def ensure_participant(conn: sqlite3.Connection, name: str, paid: int | None = 1) -> int:
    name = name.strip()
    if paid is None:
        conn.execute(
            """
            INSERT INTO participants(name, paid)
            VALUES(?, 1)
            ON CONFLICT(name) DO NOTHING
            """,
            (name,),
        )
    else:
        conn.execute(
            """
            INSERT INTO participants(name, paid)
            VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET paid = excluded.paid
        """,
        (name, paid),
    )
    row = conn.execute("SELECT id FROM participants WHERE name = ?", (name,)).fetchone()
    participant_id = int(row["id"])
    ensure_season_participant(conn, participant_id, paid)
    return participant_id


def ensure_team(conn: sqlite3.Connection, name: str, **fields: object) -> int:
    name = name.strip()
    if not fields:
        conn.execute(
            """
            INSERT INTO teams(name)
            VALUES(?)
            ON CONFLICT(name) DO NOTHING
            """,
            (name,),
        )
    else:
        columns = ["name", *fields.keys()]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
        conn.execute(
            f"""
            INSERT INTO teams({", ".join(columns)})
            VALUES({placeholders})
            ON CONFLICT(name) DO UPDATE SET {updates}
            """,
            (name, *fields.values()),
        )
    row = conn.execute("SELECT id FROM teams WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def round_sort_order(conn: sqlite3.Connection, name: str, season_id: int | None = None) -> int:
    try:
        return int(name)
    except ValueError:
        season_id = season_id or active_season_id(conn)
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next FROM rounds WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        return int(row["next"])


def ensure_round(conn: sqlite3.Connection, name: str, deadline_at: str | None = None) -> int:
    name = name.strip()
    season_id = active_season_id(conn)
    order = round_sort_order(conn, name, season_id)
    deadline = optional_iso_datetime(deadline_at)
    row = conn.execute(
        "SELECT id FROM rounds WHERE season_id = ? AND name = ?",
        (season_id, name),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO rounds(season_id, name, sort_order, deadline_at)
            VALUES(?, ?, ?, ?)
            """,
            (season_id, name, order, deadline),
        )
    else:
        round_id = int(row["id"])
        conn.execute(
            """
            UPDATE rounds
            SET sort_order = ?, deadline_at = COALESCE(?, deadline_at), season_id = ?
            WHERE id = ?
            """,
            (order, deadline, season_id, round_id),
        )
    row = conn.execute(
        "SELECT id FROM rounds WHERE season_id = ? AND name = ?",
        (season_id, name),
    ).fetchone()
    return int(row["id"])


def get_match_id(conn: sqlite3.Connection, round_name: str, position: int) -> int:
    round_id = ensure_round(conn, round_name)
    match = conn.execute(
        "SELECT id FROM matches WHERE round_id = ? AND position = ?",
        (round_id, position),
    ).fetchone()
    if match is None:
        raise ValueError(f"Unknown match: round={round_name}, position={position}")
    return int(match["id"])


def upsert_match(
    conn: sqlite3.Connection,
    round_name: str,
    position: int,
    home: str,
    away: str,
    kickoff_at: str | None,
    result: str | None,
    round_deadline_at: str | None = None,
) -> int:
    round_id = ensure_round(conn, round_name, round_deadline_at)
    ensure_team(conn, home)
    ensure_team(conn, away)
    kickoff = parse_datetime(kickoff_at).isoformat() if kickoff_at else None
    result_value = result.strip() if result and parse_score(result.strip()) else None
    conn.execute(
        """
        INSERT INTO matches(round_id, position, home, away, kickoff_at, result)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(round_id, position) DO UPDATE SET
            home = excluded.home,
            away = excluded.away,
            kickoff_at = excluded.kickoff_at,
            result = excluded.result
        """,
        (round_id, position, home.strip(), away.strip(), kickoff, result_value),
    )
    row = conn.execute(
        "SELECT id FROM matches WHERE round_id = ? AND position = ?",
        (round_id, position),
    ).fetchone()
    return int(row["id"])


def upsert_prediction(
    conn: sqlite3.Connection,
    participant: str,
    round_name: str,
    position: int,
    score: str,
    submitted_at: str | None,
    source: str | None,
) -> int:
    participant_id = ensure_participant(conn, participant, paid=None)
    round_id = ensure_round(conn, round_name)
    match = conn.execute(
        "SELECT id FROM matches WHERE round_id = ? AND position = ?",
        (round_id, position),
    ).fetchone()
    if match is None:
        raise ValueError(f"Unknown match: round={round_name}, position={position}")
    submitted = parse_datetime(submitted_at).isoformat() if submitted_at else None
    conn.execute(
        """
        INSERT INTO predictions(participant_id, match_id, score, submitted_at, source)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(participant_id, match_id) DO UPDATE SET
            score = excluded.score,
            submitted_at = excluded.submitted_at,
            source = excluded.source
        """,
        (participant_id, int(match["id"]), score.strip(), submitted, source),
    )
    row = conn.execute(
        "SELECT id FROM predictions WHERE participant_id = ? AND match_id = ?",
        (participant_id, int(match["id"])),
    ).fetchone()
    return int(row["id"])


def upsert_team(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    return ensure_team(
        conn,
        row["name"],
        short_name=optional_text(row.get("short_name")),
        country=optional_text(row.get("country")),
        confederation=optional_text(row.get("confederation")),
        fifa_rank=optional_int(row.get("fifa_rank")),
        elo_rating=optional_float(row.get("elo_rating")),
        market_value_m_eur=optional_float(row.get("market_value_m_eur")),
        manager=optional_text(row.get("manager")),
        preferred_formation=optional_text(row.get("preferred_formation")),
        attack_rating=optional_float(row.get("attack_rating")),
        defense_rating=optional_float(row.get("defense_rating")),
        transition_rating=optional_float(row.get("transition_rating")),
        set_piece_rating=optional_float(row.get("set_piece_rating")),
        goalkeeper_rating=optional_float(row.get("goalkeeper_rating")),
        style_tags=optional_text(row.get("style_tags")),
        notes=optional_text(row.get("notes")),
        updated_at=optional_iso_datetime(row.get("updated_at")),
    )


def upsert_team_form(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    team_id = ensure_team(conn, row["team"])
    conn.execute(
        """
        INSERT INTO team_form(
            team_id, match_date, opponent, venue, competition, goals_for,
            goals_against, xg_for, xg_against, result, importance, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, match_date, opponent, competition) DO UPDATE SET
            venue = excluded.venue,
            goals_for = excluded.goals_for,
            goals_against = excluded.goals_against,
            xg_for = excluded.xg_for,
            xg_against = excluded.xg_against,
            result = excluded.result,
            importance = excluded.importance,
            notes = excluded.notes
        """,
        (
            team_id,
            optional_text(row.get("match_date")),
            row["opponent"].strip(),
            optional_text(row.get("venue")),
            optional_text(row.get("competition")),
            optional_int(row.get("goals_for")),
            optional_int(row.get("goals_against")),
            optional_float(row.get("xg_for")),
            optional_float(row.get("xg_against")),
            optional_text(row.get("result")),
            optional_float(row.get("importance")),
            optional_text(row.get("notes")),
        ),
    )
    db_row = conn.execute(
        """
        SELECT id FROM team_form
        WHERE team_id = ? AND match_date = ? AND opponent = ? AND competition IS ?
        """,
        (
            team_id,
            optional_text(row.get("match_date")),
            row["opponent"].strip(),
            optional_text(row.get("competition")),
        ),
    ).fetchone()
    return int(db_row["id"])


def upsert_absence(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    team_id = ensure_team(conn, row["team"])
    conn.execute(
        """
        INSERT INTO absences(
            team_id, player, role, status, severity, impact_rating,
            expected_return, source, notes, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, player, status) DO UPDATE SET
            role = excluded.role,
            severity = excluded.severity,
            impact_rating = excluded.impact_rating,
            expected_return = excluded.expected_return,
            source = excluded.source,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            team_id,
            row["player"].strip(),
            optional_text(row.get("role")),
            row["status"].strip(),
            optional_text(row.get("severity")),
            optional_float(row.get("impact_rating")),
            optional_text(row.get("expected_return")),
            optional_text(row.get("source")),
            optional_text(row.get("notes")),
            optional_iso_datetime(row.get("updated_at")),
        ),
    )
    db_row = conn.execute(
        "SELECT id FROM absences WHERE team_id = ? AND player = ? AND status = ?",
        (team_id, row["player"].strip(), row["status"].strip()),
    ).fetchone()
    return int(db_row["id"])


def upsert_match_context(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    match_id = get_match_id(conn, row["round"], int(row["position"]))
    conn.execute(
        """
        INSERT INTO match_contexts(
            match_id, venue, city, country, neutral_site, timezone,
            home_rest_days, away_rest_days, home_travel_km, away_travel_km,
            weather, temperature_c, pitch, referee, home_motivation,
            away_motivation, home_rotation_risk, away_rotation_risk, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            venue = excluded.venue,
            city = excluded.city,
            country = excluded.country,
            neutral_site = excluded.neutral_site,
            timezone = excluded.timezone,
            home_rest_days = excluded.home_rest_days,
            away_rest_days = excluded.away_rest_days,
            home_travel_km = excluded.home_travel_km,
            away_travel_km = excluded.away_travel_km,
            weather = excluded.weather,
            temperature_c = excluded.temperature_c,
            pitch = excluded.pitch,
            referee = excluded.referee,
            home_motivation = excluded.home_motivation,
            away_motivation = excluded.away_motivation,
            home_rotation_risk = excluded.home_rotation_risk,
            away_rotation_risk = excluded.away_rotation_risk,
            notes = excluded.notes
        """,
        (
            match_id,
            optional_text(row.get("venue")),
            optional_text(row.get("city")),
            optional_text(row.get("country")),
            truthy(row.get("neutral_site")),
            optional_text(row.get("timezone")),
            optional_int(row.get("home_rest_days")),
            optional_int(row.get("away_rest_days")),
            optional_float(row.get("home_travel_km")),
            optional_float(row.get("away_travel_km")),
            optional_text(row.get("weather")),
            optional_float(row.get("temperature_c")),
            optional_text(row.get("pitch")),
            optional_text(row.get("referee")),
            optional_float(row.get("home_motivation")),
            optional_float(row.get("away_motivation")),
            optional_float(row.get("home_rotation_risk")),
            optional_float(row.get("away_rotation_risk")),
            optional_text(row.get("notes")),
        ),
    )
    return match_id


def upsert_match_odds(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    match_id = get_match_id(conn, row["round"], int(row["position"]))
    captured_at = optional_iso_datetime(row.get("captured_at"))
    if captured_at is None:
        raise ValueError("match_odds.captured_at is required")
    conn.execute(
        """
        INSERT INTO match_odds(
            match_id, bookmaker, captured_at, home_win, draw, away_win,
            over_2_5, under_2_5, btts_yes, btts_no, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id, bookmaker, captured_at) DO UPDATE SET
            home_win = excluded.home_win,
            draw = excluded.draw,
            away_win = excluded.away_win,
            over_2_5 = excluded.over_2_5,
            under_2_5 = excluded.under_2_5,
            btts_yes = excluded.btts_yes,
            btts_no = excluded.btts_no,
            notes = excluded.notes
        """,
        (
            match_id,
            row["bookmaker"].strip(),
            captured_at,
            optional_float(row.get("home_win")),
            optional_float(row.get("draw")),
            optional_float(row.get("away_win")),
            optional_float(row.get("over_2_5")),
            optional_float(row.get("under_2_5")),
            optional_float(row.get("btts_yes")),
            optional_float(row.get("btts_no")),
            optional_text(row.get("notes")),
        ),
    )
    db_row = conn.execute(
        """
        SELECT id FROM match_odds
        WHERE match_id = ? AND bookmaker = ? AND captured_at = ?
        """,
        (match_id, row["bookmaker"].strip(), captured_at),
    ).fetchone()
    return int(db_row["id"])


def upsert_team_match_factor(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    match_id = get_match_id(conn, row["round"], int(row["position"]))
    team_id = ensure_team(conn, row["team"])
    conn.execute(
        """
        INSERT INTO team_match_factors(
            match_id, team_id, side, expected_lineup_confidence,
            absences_impact, fatigue, morale, tactical_fit,
            pressing_advantage, set_piece_edge, motivation, notes
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id, team_id) DO UPDATE SET
            side = excluded.side,
            expected_lineup_confidence = excluded.expected_lineup_confidence,
            absences_impact = excluded.absences_impact,
            fatigue = excluded.fatigue,
            morale = excluded.morale,
            tactical_fit = excluded.tactical_fit,
            pressing_advantage = excluded.pressing_advantage,
            set_piece_edge = excluded.set_piece_edge,
            motivation = excluded.motivation,
            notes = excluded.notes
        """,
        (
            match_id,
            team_id,
            row["side"].strip(),
            optional_float(row.get("expected_lineup_confidence")),
            optional_float(row.get("absences_impact")),
            optional_float(row.get("fatigue")),
            optional_float(row.get("morale")),
            optional_float(row.get("tactical_fit")),
            optional_float(row.get("pressing_advantage")),
            optional_float(row.get("set_piece_edge")),
            optional_float(row.get("motivation")),
            optional_text(row.get("notes")),
        ),
    )
    return match_id


def upsert_match_assessment(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    match_id = get_match_id(conn, row["round"], int(row["position"]))
    conn.execute(
        """
        INSERT INTO match_assessments(
            match_id, suggested_score, risk_level, confidence, home_edge,
            away_edge, draw_edge, volatility, consensus_note,
            contrarian_note, notes, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            suggested_score = excluded.suggested_score,
            risk_level = excluded.risk_level,
            confidence = excluded.confidence,
            home_edge = excluded.home_edge,
            away_edge = excluded.away_edge,
            draw_edge = excluded.draw_edge,
            volatility = excluded.volatility,
            consensus_note = excluded.consensus_note,
            contrarian_note = excluded.contrarian_note,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            match_id,
            optional_text(row.get("suggested_score")),
            optional_text(row.get("risk_level")),
            optional_float(row.get("confidence")),
            optional_float(row.get("home_edge")),
            optional_float(row.get("away_edge")),
            optional_float(row.get("draw_edge")),
            optional_float(row.get("volatility")),
            optional_text(row.get("consensus_note")),
            optional_text(row.get("contrarian_note")),
            optional_text(row.get("notes")),
            optional_iso_datetime(row.get("updated_at")),
        ),
    )
    return match_id


def import_participants(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ensure_participant(conn, row["name"], truthy(row.get("paid")))
            count += 1
    conn.commit()
    return count


def import_teams(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_team(conn, row)
            count += 1
    conn.commit()
    return count


def import_matches(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_match(
                conn,
                row["round"],
                int(row["position"]),
                row["home"],
                row["away"],
                row.get("kickoff_at"),
                row.get("result"),
                row.get("round_deadline_at"),
            )
            count += 1
    conn.commit()
    return count


def import_predictions(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_prediction(
                conn,
                row["participant"],
                row["round"],
                int(row["position"]),
                row["score"],
                row.get("submitted_at"),
                row.get("source"),
            )
            count += 1
    conn.commit()
    return count


def import_team_form(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_team_form(conn, row)
            count += 1
    conn.commit()
    return count


def import_absences(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_absence(conn, row)
            count += 1
    conn.commit()
    return count


def import_match_contexts(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_match_context(conn, row)
            count += 1
    conn.commit()
    return count


def import_match_odds(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_match_odds(conn, row)
            count += 1
    conn.commit()
    return count


def import_team_match_factors(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_team_match_factor(conn, row)
            count += 1
    conn.commit()
    return count


def import_match_assessments(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_match_assessment(conn, row)
            count += 1
    conn.commit()
    return count


def find_match(conn: sqlite3.Connection, query: str) -> sqlite3.Row:
    value = query.strip()
    season_id = active_season_id(conn)
    if value.isdigit():
        row = conn.execute(
            """
            SELECT m.*, r.name AS round_name, r.sort_order
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND m.position = ?
            ORDER BY r.sort_order, m.position
            LIMIT 1
            """,
            (season_id, int(value)),
        ).fetchone()
        if row:
            return row

    like = f"%{value}%"
    row = conn.execute(
        """
        SELECT m.*, r.name AS round_name, r.sort_order
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
          AND (
            (m.home || ' ' || m.away) LIKE ?
            OR (m.home || ' - ' || m.away) LIKE ?
            OR (m.home || ' — ' || m.away) LIKE ?
          )
        ORDER BY r.sort_order, m.position
        LIMIT 1
        """,
        (season_id, like, like, like),
    ).fetchone()
    if row is None:
        raise ValueError(f"Match not found: {query}")
    return row
