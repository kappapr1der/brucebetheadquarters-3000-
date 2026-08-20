from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Iterable, TYPE_CHECKING

from .scoring import parse_datetime, parse_score

if TYPE_CHECKING:
    from .vk_board import VkDiscoveredTopic
    from .vk_dry_run import VkRegistrationEntry


DEFAULT_COMPETITION_CODE = "epl"
DEFAULT_COMPETITION_NAME = "English Premier League"
DEFAULT_SEASON_NAME = "2026/27"
LEGACY_COMPETITION_CODE = "legacy"
LEGACY_COMPETITION_NAME = "Legacy imported data"
LEGACY_SEASON_NAME = "pre-season-model"
VK_UI_NOISE_PARTICIPANTS = frozenset(
    {
        "show likes",
        "show reactions",
        "show more posts",
        "загружается",
        "показать список оценивших",
        "показать реакции",
        "reply",
        "share",
        "ответить",
        "поделиться",
    }
)


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

CREATE TABLE IF NOT EXISTS player_status_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player TEXT NOT NULL,
    role TEXT,
    status TEXT,
    availability_pct REAL,
    form_rating REAL,
    minutes_last_5 INTEGER,
    starts_last_5 INTEGER,
    goals_last_5 REAL,
    assists_last_5 REAL,
    xg_last_5 REAL,
    xa_last_5 REAL,
    source TEXT,
    source_ref TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(team_id, player, source, updated_at)
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

CREATE TABLE IF NOT EXISTS result_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    fixtures_seen INTEGER NOT NULL DEFAULT 0,
    finished_seen INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    unmatched INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS manual_result_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    previous_result TEXT,
    new_result TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    actor_chat_id INTEGER,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS manual_prediction_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    previous_score TEXT NOT NULL,
    previous_submitted_at TEXT,
    previous_source TEXT,
    new_score TEXT NOT NULL,
    new_submitted_at TEXT,
    changed_at TEXT NOT NULL,
    actor_chat_id INTEGER,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS round_reviews (
    round_id INTEGER PRIMARY KEY REFERENCES rounds(id) ON DELETE CASCADE,
    completed_at TEXT NOT NULL,
    match_count INTEGER NOT NULL,
    finished_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    model_key TEXT NOT NULL,
    suggested_score TEXT NOT NULL,
    confidence REAL,
    risk_level TEXT,
    captured_at TEXT NOT NULL,
    assessment_updated_at TEXT,
    UNIQUE(match_id, model_key)
);

CREATE TABLE IF NOT EXISTS reminder_subscriptions (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    reminder_key TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    error TEXT,
    UNIQUE(chat_id, round_id, reminder_key)
);

CREATE TABLE IF NOT EXISTS vk_topic_discovery_state (
    group_id INTEGER PRIMARY KEY,
    initialized_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vk_topic_alerts (
    group_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    topic_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'pending',
    notified_at TEXT,
    notification_error TEXT,
    PRIMARY KEY(group_id, topic_id)
);

CREATE TABLE IF NOT EXISTS vk_registration_entries (
    group_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    source_key TEXT NOT NULL,
    participant_id INTEGER NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    vk_author TEXT NOT NULL,
    participant_name TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    fee_intent TEXT NOT NULL,
    fee_amount_rub INTEGER,
    payment_status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'pending',
    notified_at TEXT,
    notification_error TEXT,
    PRIMARY KEY(group_id, topic_id, source_key)
);
"""


@dataclass(frozen=True)
class VkTopicAlert:
    group_id: int
    topic_id: int
    topic_kind: str
    title: str
    url: str
    first_seen_at: str


@dataclass(frozen=True)
class VkRegistrationAlert:
    group_id: int
    topic_id: int
    source_key: str
    participant_name: str
    vk_author: str
    submitted_at: str
    fee_intent: str
    fee_amount_rub: int | None
    payment_status: str


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate_db(conn)
    activate_profile(conn)
    _purge_vk_ui_noise_entries(conn)
    _dedupe_vk_registration_entries(conn)
    conn.commit()


def reset_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;
        DROP TABLE IF EXISTS match_assessments;
        DROP TABLE IF EXISTS reminder_deliveries;
        DROP TABLE IF EXISTS reminder_subscriptions;
        DROP TABLE IF EXISTS vk_topic_alerts;
        DROP TABLE IF EXISTS vk_topic_discovery_state;
        DROP TABLE IF EXISTS vk_registration_entries;
        DROP TABLE IF EXISTS model_forecasts;
        DROP TABLE IF EXISTS round_reviews;
        DROP TABLE IF EXISTS result_sync_runs;
        DROP TABLE IF EXISTS manual_result_overrides;
        DROP TABLE IF EXISTS manual_prediction_overrides;
        DROP TABLE IF EXISTS team_match_factors;
        DROP TABLE IF EXISTS match_odds;
        DROP TABLE IF EXISTS match_contexts;
        DROP TABLE IF EXISTS absences;
        DROP TABLE IF EXISTS player_status_snapshots;
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


def record_vk_topic_discovery(
    conn: sqlite3.Connection,
    group_id: int,
    candidates: Iterable["VkDiscoveredTopic"],
    *,
    checked_at: str,
) -> tuple[bool, list[VkTopicAlert]]:
    """Persist discovery state and return candidate topics that still need a Telegram alert.

    The first successful pass is a baseline. It deliberately produces no alert,
    so old discussions cannot be mistaken for newly opened EPL topics.
    """

    observed = list(candidates)
    # A blank render can mean that VK changed its page shell. Do not establish
    # a baseline until the browser has actually exposed at least one topic link.
    if not observed:
        return False, []
    normalized = [item for item in observed if item.is_epl_candidate]
    state = conn.execute(
        "SELECT group_id FROM vk_topic_discovery_state WHERE group_id = ?",
        (int(group_id),),
    ).fetchone()
    baseline = state is None

    for item in normalized:
        status = "baseline" if baseline else "pending"
        conn.execute(
            """
            INSERT INTO vk_topic_alerts(
                group_id, topic_id, topic_kind, title, url,
                first_seen_at, last_seen_at, notification_status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, topic_id) DO UPDATE SET
                topic_kind = excluded.topic_kind,
                title = excluded.title,
                url = excluded.url,
                last_seen_at = excluded.last_seen_at
            """,
            (
                int(item.group_id),
                int(item.topic_id),
                item.topic_kind,
                item.title,
                item.url,
                checked_at,
                checked_at,
                status,
            ),
        )

    if baseline:
        conn.execute(
            """
            INSERT INTO vk_topic_discovery_state(group_id, initialized_at, last_checked_at)
            VALUES(?, ?, ?)
            """,
            (int(group_id), checked_at, checked_at),
        )
        conn.commit()
        return True, []

    conn.execute(
        "UPDATE vk_topic_discovery_state SET last_checked_at = ? WHERE group_id = ?",
        (checked_at, int(group_id)),
    )
    rows = conn.execute(
        """
        SELECT group_id, topic_id, topic_kind, title, url, first_seen_at
        FROM vk_topic_alerts
        WHERE group_id = ? AND notification_status = 'pending'
        ORDER BY first_seen_at, topic_id
        """,
        (int(group_id),),
    ).fetchall()
    conn.commit()
    return False, [
        VkTopicAlert(
            group_id=int(row["group_id"]),
            topic_id=int(row["topic_id"]),
            topic_kind=row["topic_kind"],
            title=row["title"],
            url=row["url"],
            first_seen_at=row["first_seen_at"],
        )
        for row in rows
    ]


def mark_vk_topic_alert_sent(conn: sqlite3.Connection, alert: VkTopicAlert, *, sent_at: str) -> None:
    conn.execute(
        """
        UPDATE vk_topic_alerts
        SET notification_status = 'sent', notified_at = ?, notification_error = NULL
        WHERE group_id = ? AND topic_id = ?
        """,
        (sent_at, alert.group_id, alert.topic_id),
    )
    conn.commit()


def mark_vk_topic_alert_failed(conn: sqlite3.Connection, alert: VkTopicAlert, error: str) -> None:
    conn.execute(
        """
        UPDATE vk_topic_alerts
        SET notification_status = 'pending', notification_error = ?
        WHERE group_id = ? AND topic_id = ?
        """,
        (error[:500], alert.group_id, alert.topic_id),
    )
    conn.commit()


def _ensure_vk_registration_participant(
    conn: sqlite3.Connection,
    name: str,
    fee_intent: str,
) -> int:
    """Enroll a VK applicant using the registration topic as the fee record."""

    confirmed_paid = {"paid_declared": 1, "free": 0}.get(fee_intent)
    if confirmed_paid is not None:
        return ensure_participant(conn, name, paid=confirmed_paid)

    row = conn.execute("SELECT id FROM participants WHERE name = ?", (name,)).fetchone()
    if row is None:
        return ensure_participant(conn, name, paid=0)

    participant_id = int(row["id"])
    season_row = conn.execute(
        "SELECT paid FROM season_participants WHERE season_id = ? AND participant_id = ?",
        (active_season_id(conn), participant_id),
    ).fetchone()
    if season_row is None:
        ensure_season_participant(conn, participant_id, paid=0)
    return participant_id


def _is_vk_ui_noise_participant(name: str) -> bool:
    return " ".join(name.casefold().split()) in VK_UI_NOISE_PARTICIPANTS


def _purge_vk_ui_noise_entries(conn: sqlite3.Connection) -> None:
    """Remove legacy rows that came from VK controls, never from a person."""

    rows = conn.execute(
        "SELECT rowid, participant_id, participant_name FROM vk_registration_entries"
    ).fetchall()
    noise_rows = [row for row in rows if _is_vk_ui_noise_participant(str(row["participant_name"]))]
    if not noise_rows:
        return

    participant_ids = {int(row["participant_id"]) for row in noise_rows}
    conn.executemany("DELETE FROM vk_registration_entries WHERE rowid = ?", [(int(row["rowid"]),) for row in noise_rows])
    for participant_id in participant_ids:
        prediction = conn.execute("SELECT 1 FROM predictions WHERE participant_id = ? LIMIT 1", (participant_id,)).fetchone()
        registration = conn.execute(
            "SELECT 1 FROM vk_registration_entries WHERE participant_id = ? LIMIT 1", (participant_id,)
        ).fetchone()
        if prediction is None and registration is None:
            conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))


def _dedupe_vk_registration_entries(conn: sqlite3.Connection) -> None:
    """Keep one imported row when a parser revision changes a legacy source key."""

    rows = conn.execute(
        """
        SELECT rowid, group_id, topic_id, participant_id, vk_author, submitted_at,
               last_seen_at, notification_status, notified_at
        FROM vk_registration_entries
        ORDER BY last_seen_at DESC, rowid DESC
        """
    ).fetchall()
    retained: dict[tuple[int, int, str, str], sqlite3.Row] = {}
    obsolete_rows: list[sqlite3.Row] = []
    notification_updates: list[tuple[str | None, int]] = []

    for row in rows:
        key = (int(row["group_id"]), int(row["topic_id"]), str(row["vk_author"]), str(row["submitted_at"]))
        current = retained.get(key)
        if current is None:
            retained[key] = row
            continue
        if row["notification_status"] == "sent" and current["notification_status"] != "sent":
            notification_updates.append((row["notified_at"], int(current["rowid"])))
        obsolete_rows.append(row)

    if not obsolete_rows:
        return

    conn.executemany("DELETE FROM vk_registration_entries WHERE rowid = ?", [(int(row["rowid"]),) for row in obsolete_rows])
    conn.executemany(
        "UPDATE vk_registration_entries SET notification_status = 'sent', notified_at = ?, notification_error = NULL WHERE rowid = ?",
        notification_updates,
    )
    candidate_ids = {int(row["participant_id"]) for row in obsolete_rows}
    for participant_id in candidate_ids:
        prediction = conn.execute("SELECT 1 FROM predictions WHERE participant_id = ? LIMIT 1", (participant_id,)).fetchone()
        registration = conn.execute(
            "SELECT 1 FROM vk_registration_entries WHERE participant_id = ? LIMIT 1", (participant_id,)
        ).fetchone()
        if prediction is None and registration is None:
            conn.execute("DELETE FROM participants WHERE id = ?", (participant_id,))


def record_vk_registration_entries(
    conn: sqlite3.Connection,
    group_id: int,
    topic_id: int,
    entries: Iterable["VkRegistrationEntry"],
    *,
    seen_at: str,
) -> list[VkRegistrationAlert]:
    """Store registration declarations and return only undelivered Telegram notices.

    The registration discussion is the contest's source of truth: a declared
    fee immediately marks the participant as paid for this season.
    """

    _purge_vk_ui_noise_entries(conn)
    _dedupe_vk_registration_entries(conn)
    for entry in entries:
        if _is_vk_ui_noise_participant(entry.participant):
            continue
        participant_id = _ensure_vk_registration_participant(conn, entry.participant, entry.fee_intent)
        submitted_at = entry.submitted_at.isoformat()
        existing = conn.execute(
            """
            SELECT source_key, vk_author, participant_name, submitted_at, fee_intent,
                   fee_amount_rub, payment_status
            FROM vk_registration_entries
            WHERE group_id = ? AND topic_id = ?
              AND (source_key = ? OR (vk_author = ? AND submitted_at = ?))
            ORDER BY CASE WHEN source_key = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (int(group_id), int(topic_id), entry.source_key, entry.vk_author, submitted_at, entry.source_key),
        ).fetchone()
        values = (
            entry.vk_author,
            entry.participant,
            submitted_at,
            entry.fee_intent,
            entry.fee_amount_rub,
            entry.payment_status,
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO vk_registration_entries(
                    group_id, topic_id, source_key, participant_id, vk_author,
                    participant_name, submitted_at, fee_intent, fee_amount_rub,
                    payment_status, first_seen_at, last_seen_at, notification_status
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (int(group_id), int(topic_id), entry.source_key, participant_id, *values, seen_at, seen_at),
            )
            continue

        existing_source_key = str(existing["source_key"])
        previous = tuple(existing[column] for column in ("vk_author", "participant_name", "submitted_at", "fee_intent", "fee_amount_rub", "payment_status"))
        if previous != values:
            conn.execute(
                """
                UPDATE vk_registration_entries
                SET source_key = ?, participant_id = ?, vk_author = ?, participant_name = ?, submitted_at = ?,
                    fee_intent = ?, fee_amount_rub = ?, payment_status = ?,
                    last_seen_at = ?, notification_status = 'pending',
                    notified_at = NULL, notification_error = NULL
                WHERE group_id = ? AND topic_id = ? AND source_key = ?
                """,
                (entry.source_key, participant_id, *values, seen_at, int(group_id), int(topic_id), existing_source_key),
            )
        else:
            conn.execute(
                """
                UPDATE vk_registration_entries
                SET source_key = ?, participant_id = ?, last_seen_at = ?
                WHERE group_id = ? AND topic_id = ? AND source_key = ?
                """,
                (entry.source_key, participant_id, seen_at, int(group_id), int(topic_id), existing_source_key),
            )

    rows = conn.execute(
        """
        SELECT group_id, topic_id, source_key, participant_name, vk_author,
               submitted_at, fee_intent, fee_amount_rub, payment_status
        FROM vk_registration_entries
        WHERE group_id = ? AND topic_id = ? AND notification_status = 'pending'
        ORDER BY submitted_at, source_key
        """,
        (int(group_id), int(topic_id)),
    ).fetchall()
    conn.commit()
    return [
        VkRegistrationAlert(
            group_id=int(row["group_id"]),
            topic_id=int(row["topic_id"]),
            source_key=row["source_key"],
            participant_name=row["participant_name"],
            vk_author=row["vk_author"],
            submitted_at=row["submitted_at"],
            fee_intent=row["fee_intent"],
            fee_amount_rub=row["fee_amount_rub"],
            payment_status=row["payment_status"],
        )
        for row in rows
    ]


def mark_vk_registration_alert_sent(
    conn: sqlite3.Connection,
    alert: VkRegistrationAlert,
    *,
    sent_at: str,
) -> None:
    conn.execute(
        """
        UPDATE vk_registration_entries
        SET notification_status = 'sent', notified_at = ?, notification_error = NULL
        WHERE group_id = ? AND topic_id = ? AND source_key = ?
        """,
        (sent_at, alert.group_id, alert.topic_id, alert.source_key),
    )
    conn.commit()


def mark_vk_registration_alert_failed(
    conn: sqlite3.Connection,
    alert: VkRegistrationAlert,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE vk_registration_entries
        SET notification_status = 'pending', notification_error = ?
        WHERE group_id = ? AND topic_id = ? AND source_key = ?
        """,
        (error[:500], alert.group_id, alert.topic_id, alert.source_key),
    )
    conn.commit()


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


def set_manual_match_result(
    conn: sqlite3.Connection,
    match_id: int,
    result: str,
    actor_chat_id: int | None = None,
    reason: str | None = None,
    changed_at: str | None = None,
) -> tuple[str | None, str]:
    """Set a validated fallback result and retain an audit record for every change."""
    parsed = parse_score(result)
    if parsed is None:
        raise ValueError("Result must be a one-digit score such as 2:1")
    row = conn.execute("SELECT result FROM matches WHERE id = ?", (match_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown match id: {match_id}")
    previous = row["result"]
    normalized = parsed.label()
    timestamp = parse_datetime(changed_at).isoformat() if changed_at else datetime.now().astimezone().isoformat()
    conn.execute("UPDATE matches SET result = ? WHERE id = ?", (normalized, match_id))
    conn.execute(
        """
        INSERT INTO manual_result_overrides(
            match_id, previous_result, new_result, changed_at, actor_chat_id, reason
        )
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (match_id, previous, normalized, timestamp, actor_chat_id, optional_text(reason)),
    )
    conn.commit()
    return previous, normalized


def manual_result_history(conn: sqlite3.Connection, match_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT previous_result, new_result, changed_at, actor_chat_id, reason
            FROM manual_result_overrides
            WHERE match_id = ?
            ORDER BY id DESC
            """,
            (match_id,),
        )
    )


def effective_round_deadline(
    conn: sqlite3.Connection,
    round_name: str,
    lock_minutes: int = 90,
) -> datetime | None:
    """Match the contest rule: the round closes before its first kickoff."""
    row = conn.execute(
        """
        SELECT r.deadline_at, MIN(m.kickoff_at) AS first_kickoff_at
        FROM rounds r
        LEFT JOIN matches m ON m.round_id = r.id
        WHERE r.season_id = ? AND r.name = ?
        GROUP BY r.id
        """,
        (active_season_id(conn), round_name.strip()),
    ).fetchone()
    if row is None:
        return None
    first_kickoff_at = parse_datetime(row["first_kickoff_at"])
    if first_kickoff_at is not None:
        return first_kickoff_at - timedelta(minutes=lock_minutes)
    return parse_datetime(row["deadline_at"])


def prediction_update_is_locked(
    conn: sqlite3.Connection,
    participant: str,
    round_name: str,
    position: int,
    submitted_at: datetime | str | None,
    lock_minutes: int = 90,
) -> bool:
    """Keep a stored forecast intact when a replacement arrives after the lock.

    New late forecasts remain visible for the contest's partial-late handling.
    Only replacing an existing forecast is blocked here; a deliberate correction
    must go through ``set_manual_prediction_override`` and leaves an audit row.
    """
    raw_timestamp = submitted_at.isoformat() if isinstance(submitted_at, datetime) else submitted_at
    incoming = parse_datetime(raw_timestamp)
    deadline = effective_round_deadline(conn, round_name, lock_minutes=lock_minutes)
    if incoming is None or deadline is None or incoming <= deadline:
        return False
    row = conn.execute(
        """
        SELECT pr.id
        FROM predictions pr
        JOIN participants p ON p.id = pr.participant_id
        JOIN matches m ON m.id = pr.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
          AND r.name = ?
          AND m.position = ?
          AND lower(p.name) = lower(?)
        """,
        (active_season_id(conn), round_name.strip(), position, participant.strip()),
    ).fetchone()
    return row is not None


def set_manual_prediction_override(
    conn: sqlite3.Connection,
    participant: str,
    match_id: int,
    score: str,
    actor_chat_id: int | None = None,
    reason: str | None = None,
    changed_at: str | None = None,
    submitted_at: str | None = None,
) -> tuple[str, str]:
    """Correct one stored forecast explicitly and retain the full correction trail."""
    parsed = parse_score(score)
    if parsed is None:
        raise ValueError("Forecast must be a one-digit score such as 2:1")
    row = conn.execute(
        """
        SELECT pr.id, pr.score, pr.submitted_at, pr.source
        FROM predictions pr
        JOIN participants p ON p.id = pr.participant_id
        WHERE pr.match_id = ? AND lower(p.name) = lower(?)
        """,
        (match_id, participant.strip()),
    ).fetchone()
    if row is None:
        raise ValueError(f"Stored forecast not found for {participant}")

    normalized = parsed.label()
    timestamp = parse_datetime(changed_at).isoformat() if changed_at else datetime.now().astimezone().isoformat()
    preserved_submission = parse_datetime(submitted_at).isoformat() if submitted_at else row["submitted_at"]
    conn.execute(
        """
        UPDATE predictions
        SET score = ?, submitted_at = ?, source = ?
        WHERE id = ?
        """,
        (normalized, preserved_submission, "manual-override", int(row["id"])),
    )
    conn.execute(
        """
        INSERT INTO manual_prediction_overrides(
            prediction_id, previous_score, previous_submitted_at, previous_source,
            new_score, new_submitted_at, changed_at, actor_chat_id, reason
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["id"]),
            row["score"],
            row["submitted_at"],
            row["source"],
            normalized,
            preserved_submission,
            timestamp,
            actor_chat_id,
            optional_text(reason) or "manual forecast correction",
        ),
    )
    conn.commit()
    return str(row["score"]), normalized


def manual_prediction_history(conn: sqlite3.Connection, participant: str, match_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                mpo.previous_score,
                mpo.previous_submitted_at,
                mpo.previous_source,
                mpo.new_score,
                mpo.new_submitted_at,
                mpo.changed_at,
                mpo.actor_chat_id,
                mpo.reason
            FROM manual_prediction_overrides mpo
            JOIN predictions pr ON pr.id = mpo.prediction_id
            JOIN participants p ON p.id = pr.participant_id
            WHERE pr.match_id = ? AND lower(p.name) = lower(?)
            ORDER BY mpo.id DESC
            """,
            (match_id, participant.strip()),
        )
    )


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
    player = row["player"].strip()
    status = row["status"].strip().lower()
    if not player or not status:
        raise ValueError("absence.player and absence.status are required")
    if status in {"available", "fit", "cleared", "returned"}:
        conn.execute(
            "DELETE FROM absences WHERE team_id = ? AND lower(player) = lower(?)",
            (team_id, player),
        )
        return 0
    # This is a current availability register, not a history table. A new status
    # supersedes the old one so an "injured" row cannot linger after an update.
    conn.execute(
        "DELETE FROM absences WHERE team_id = ? AND lower(player) = lower(?) AND lower(status) <> ?",
        (team_id, player, status),
    )
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
            player,
            optional_text(row.get("role")),
            status,
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
        (team_id, player, status),
    ).fetchone()
    return int(db_row["id"])


def upsert_player_status(conn: sqlite3.Connection, row: dict[str, str]) -> int:
    team_id = ensure_team(conn, row["team"])
    updated_at = optional_iso_datetime(row.get("updated_at"))
    if updated_at is None:
        raise ValueError("player_status.updated_at is required")
    source = optional_text(row.get("source")) or "manual"
    conn.execute(
        """
        INSERT INTO player_status_snapshots(
            team_id, player, role, status, availability_pct, form_rating,
            minutes_last_5, starts_last_5, goals_last_5, assists_last_5,
            xg_last_5, xa_last_5, source, source_ref, notes, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, player, source, updated_at) DO UPDATE SET
            role = excluded.role,
            status = excluded.status,
            availability_pct = excluded.availability_pct,
            form_rating = excluded.form_rating,
            minutes_last_5 = excluded.minutes_last_5,
            starts_last_5 = excluded.starts_last_5,
            goals_last_5 = excluded.goals_last_5,
            assists_last_5 = excluded.assists_last_5,
            xg_last_5 = excluded.xg_last_5,
            xa_last_5 = excluded.xa_last_5,
            source_ref = excluded.source_ref,
            notes = excluded.notes
        """,
        (
            team_id,
            row["player"].strip(),
            optional_text(row.get("role")),
            optional_text(row.get("status")),
            optional_float(row.get("availability_pct")),
            optional_float(row.get("form_rating")),
            optional_int(row.get("minutes_last_5")),
            optional_int(row.get("starts_last_5")),
            optional_float(row.get("goals_last_5")),
            optional_float(row.get("assists_last_5")),
            optional_float(row.get("xg_last_5")),
            optional_float(row.get("xa_last_5")),
            source,
            optional_text(row.get("source_ref")),
            optional_text(row.get("notes")),
            updated_at,
        ),
    )
    db_row = conn.execute(
        """
        SELECT id FROM player_status_snapshots
        WHERE team_id = ? AND player = ? AND source = ? AND updated_at = ?
        """,
        (team_id, row["player"].strip(), source, updated_at),
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


def import_player_statuses(conn: sqlite3.Connection, path: str | Path) -> int:
    count = 0
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            upsert_player_status(conn, row)
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

