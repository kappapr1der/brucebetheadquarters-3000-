from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable

from .storage import active_season


@dataclass(frozen=True)
class SnapshotTable:
    filename: str
    query: str
    params: tuple[object, ...] = ()


@dataclass(frozen=True)
class SnapshotResult:
    out_dir: Path
    manifest_path: Path
    generated_at: str
    season: str
    tables: dict[str, int]


def value_for_csv(value: object) -> object:
    return "" if value is None else value


def write_csv(conn: sqlite3.Connection, table: SnapshotTable, out_dir: Path) -> int:
    cursor = conn.execute(table.query, table.params)
    headers = [item[0] for item in cursor.description]
    rows = cursor.fetchall()
    path = out_dir / table.filename
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([value_for_csv(row[header]) for header in headers])
    return len(rows)


def active_season_snapshot_tables(season_id: int) -> Iterable[SnapshotTable]:
    yield SnapshotTable(
        "season.csv",
        """
        SELECT
            c.code AS competition_code,
            c.name AS competition_name,
            s.name AS season,
            s.display_name,
            s.entry_fee_rub,
            s.payout_first,
            s.payout_second,
            s.payout_third,
            s.deadline_lock_minutes,
            s.notes
        FROM seasons s
        JOIN competitions c ON c.id = s.competition_id
        WHERE s.id = ?
        ORDER BY c.code, s.name
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "participants.csv",
        """
        SELECT
            p.name,
            sp.paid,
            sp.active,
            sp.alias,
            sp.notes
        FROM season_participants sp
        JOIN participants p ON p.id = sp.participant_id
        WHERE sp.season_id = ?
        ORDER BY p.name
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "rounds.csv",
        """
        SELECT name, sort_order, deadline_at
        FROM rounds
        WHERE season_id = ?
        ORDER BY sort_order, name
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "matches.csv",
        """
        SELECT
            r.name AS round,
            m.position,
            m.home,
            m.away,
            m.kickoff_at,
            r.deadline_at AS round_deadline_at,
            m.result
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "predictions.csv",
        """
        SELECT
            p.name AS participant,
            r.name AS round,
            m.position,
            m.home,
            m.away,
            pr.score,
            pr.submitted_at,
            pr.source
        FROM predictions pr
        JOIN participants p ON p.id = pr.participant_id
        JOIN matches m ON m.id = pr.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position, p.name
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "teams.csv",
        """
        SELECT
            name,
            short_name,
            country,
            confederation,
            fifa_rank,
            elo_rating,
            market_value_m_eur,
            manager,
            preferred_formation,
            attack_rating,
            defense_rating,
            transition_rating,
            set_piece_rating,
            goalkeeper_rating,
            style_tags,
            notes,
            updated_at
        FROM teams
        ORDER BY name
        """,
    )
    yield SnapshotTable(
        "team_form.csv",
        """
        SELECT
            t.name AS team,
            tf.match_date,
            tf.opponent,
            tf.venue,
            tf.competition,
            tf.goals_for,
            tf.goals_against,
            tf.xg_for,
            tf.xg_against,
            tf.result,
            tf.importance,
            tf.notes
        FROM team_form tf
        JOIN teams t ON t.id = tf.team_id
        ORDER BY t.name, tf.match_date, tf.opponent
        """,
    )
    yield SnapshotTable(
        "absences.csv",
        """
        SELECT
            t.name AS team,
            a.player,
            a.role,
            a.status,
            a.severity,
            a.impact_rating,
            a.expected_return,
            a.source,
            a.notes,
            a.updated_at
        FROM absences a
        JOIN teams t ON t.id = a.team_id
        ORDER BY t.name, a.player, a.status
        """,
    )
    yield SnapshotTable(
        "player_status_snapshots.csv",
        """
        SELECT
            t.name AS team,
            ps.player,
            ps.role,
            ps.status,
            ps.availability_pct,
            ps.form_rating,
            ps.minutes_last_5,
            ps.starts_last_5,
            ps.goals_last_5,
            ps.assists_last_5,
            ps.xg_last_5,
            ps.xa_last_5,
            ps.source,
            ps.source_ref,
            ps.notes,
            ps.updated_at
        FROM player_status_snapshots ps
        JOIN teams t ON t.id = ps.team_id
        ORDER BY t.name, ps.player, ps.source, ps.updated_at
        """,
    )
    yield SnapshotTable(
        "match_contexts.csv",
        """
        SELECT
            r.name AS round,
            m.position,
            m.home,
            m.away,
            mc.venue,
            mc.city,
            mc.country,
            mc.neutral_site,
            mc.timezone,
            mc.home_rest_days,
            mc.away_rest_days,
            mc.home_travel_km,
            mc.away_travel_km,
            mc.weather,
            mc.temperature_c,
            mc.pitch,
            mc.referee,
            mc.home_motivation,
            mc.away_motivation,
            mc.home_rotation_risk,
            mc.away_rotation_risk,
            mc.notes
        FROM match_contexts mc
        JOIN matches m ON m.id = mc.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "match_odds.csv",
        """
        SELECT
            r.name AS round,
            m.position,
            m.home,
            m.away,
            mo.bookmaker,
            mo.captured_at,
            mo.home_win,
            mo.draw,
            mo.away_win,
            mo.over_2_5,
            mo.under_2_5,
            mo.btts_yes,
            mo.btts_no,
            mo.notes
        FROM match_odds mo
        JOIN matches m ON m.id = mo.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position, mo.bookmaker, mo.captured_at
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "team_match_factors.csv",
        """
        SELECT
            r.name AS round,
            m.position,
            m.home,
            m.away,
            t.name AS team,
            tmf.side,
            tmf.expected_lineup_confidence,
            tmf.absences_impact,
            tmf.fatigue,
            tmf.morale,
            tmf.tactical_fit,
            tmf.pressing_advantage,
            tmf.set_piece_edge,
            tmf.motivation,
            tmf.notes
        FROM team_match_factors tmf
        JOIN teams t ON t.id = tmf.team_id
        JOIN matches m ON m.id = tmf.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position, tmf.side, t.name
        """,
        (season_id,),
    )
    yield SnapshotTable(
        "match_assessments.csv",
        """
        SELECT
            r.name AS round,
            m.position,
            m.home,
            m.away,
            ma.suggested_score,
            ma.risk_level,
            ma.confidence,
            ma.home_edge,
            ma.away_edge,
            ma.draw_edge,
            ma.volatility,
            ma.consensus_note,
            ma.contrarian_note,
            ma.notes,
            ma.updated_at
        FROM match_assessments ma
        JOIN matches m ON m.id = ma.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position
        """,
        (season_id,),
    )


def export_snapshot(conn: sqlite3.Connection, out_dir: str | Path, label: str | None = None) -> SnapshotResult:
    season = active_season(conn)
    season_id = int(season["id"])
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    table_counts: dict[str, int] = {}
    for table in active_season_snapshot_tables(season_id):
        table_counts[table.filename] = write_csv(conn, table, target)

    manifest = {
        "label": label or "",
        "scope": "active_season",
        "competition_code": season["competition_code"],
        "season": season["name"],
        "display_name": season["display_name"],
        "tables": table_counts,
    }
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SnapshotResult(
        out_dir=target,
        manifest_path=manifest_path,
        generated_at=generated_at,
        season=str(season["display_name"] or season["name"]),
        tables=table_counts,
    )
