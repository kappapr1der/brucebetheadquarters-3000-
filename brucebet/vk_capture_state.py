from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Iterable

from .vk_dry_run import VkTopicDryRunReport
from .vk_prediction_notifications import enqueue_vk_prediction_notification


@dataclass(frozen=True)
class VkCaptureState:
    group_id: int
    topic_id: int
    topic_kind: str
    capture_complete: bool
    stop_reason: str
    pages_fetched: int
    round_names: tuple[str, ...]
    last_captured_at: str
    last_complete_at: str | None


def _round_names(report: VkTopicDryRunReport) -> tuple[str, ...]:
    return tuple(sorted({item.round_name.strip() for item in report.templates if item.round_name.strip()}))


def record_vk_capture_state(
    conn: sqlite3.Connection,
    report: VkTopicDryRunReport,
    *,
    score_line_count: int,
    notification_chat_ids: Iterable[int] = (),
) -> tuple[VkCaptureState, bool]:
    """Persist only sanitized capture diagnostics and one deduplicated warning."""

    round_names = _round_names(report)
    existing = conn.execute(
        """
        SELECT last_complete_at, last_complete_fingerprint
        FROM vk_topic_capture_state
        WHERE group_id = ? AND topic_id = ? AND topic_kind = ?
        """,
        (report.group_id, report.topic_id, report.topic_kind),
    ).fetchone()
    last_complete_at = str(existing["last_complete_at"]) if existing and existing["last_complete_at"] else None
    last_complete_fingerprint = (
        str(existing["last_complete_fingerprint"])
        if existing and existing["last_complete_fingerprint"]
        else None
    )
    if report.capture_complete:
        last_complete_at = report.captured_at.isoformat()
        last_complete_fingerprint = report.content_fingerprint

    conn.execute(
        """
        INSERT INTO vk_topic_capture_state(
            group_id, topic_id, topic_kind, last_captured_at, capture_complete,
            stop_reason, pages_fetched, submissions_seen, score_line_count,
            round_names_json, earliest_submitted_at, latest_submitted_at,
            warnings_json, content_fingerprint, last_complete_at, last_complete_fingerprint
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, topic_id, topic_kind) DO UPDATE SET
            last_captured_at = excluded.last_captured_at,
            capture_complete = excluded.capture_complete,
            stop_reason = excluded.stop_reason,
            pages_fetched = excluded.pages_fetched,
            submissions_seen = excluded.submissions_seen,
            score_line_count = excluded.score_line_count,
            round_names_json = excluded.round_names_json,
            earliest_submitted_at = excluded.earliest_submitted_at,
            latest_submitted_at = excluded.latest_submitted_at,
            warnings_json = excluded.warnings_json,
            content_fingerprint = excluded.content_fingerprint,
            last_complete_at = excluded.last_complete_at,
            last_complete_fingerprint = excluded.last_complete_fingerprint
        """,
        (
            report.group_id,
            report.topic_id,
            report.topic_kind,
            report.captured_at.isoformat(),
            int(report.capture_complete),
            report.capture_stop_reason,
            len(report.capture_page_keys),
            len(report.forecast_submissions),
            int(score_line_count),
            json.dumps(round_names, ensure_ascii=False),
            report.capture_earliest_submitted_at.isoformat() if report.capture_earliest_submitted_at else None,
            report.capture_latest_submitted_at.isoformat() if report.capture_latest_submitted_at else None,
            json.dumps(report.capture_warnings, ensure_ascii=False),
            report.content_fingerprint,
            last_complete_at,
            last_complete_fingerprint,
        ),
    )

    warning_created = False
    if not report.capture_complete:
        scope = ",".join(round_names) or "unknown"
        warning_created = enqueue_vk_prediction_notification(
            conn,
            kind="field_incomplete",
            group_id=report.group_id,
            topic_id=report.topic_id,
            source_key=f"capture:{scope}:{report.capture_stop_reason}",
            content_fingerprint="capture-incomplete-v1",
            participant_name="VK capture",
            vk_author="Forecasters Club",
            round_name=scope,
            payload={
                "reason": report.capture_stop_reason,
                "last_complete_at": last_complete_at or "нет полного снимка",
            },
            chat_ids=notification_chat_ids,
            created_at=report.captured_at.isoformat(),
        )
    conn.commit()
    return (
        VkCaptureState(
            group_id=report.group_id,
            topic_id=report.topic_id,
            topic_kind=report.topic_kind,
            capture_complete=report.capture_complete,
            stop_reason=report.capture_stop_reason,
            pages_fetched=len(report.capture_page_keys),
            round_names=round_names,
            last_captured_at=report.captured_at.isoformat(),
            last_complete_at=last_complete_at,
        ),
        warning_created,
    )


def capture_gate_for_round(conn: sqlite3.Connection, round_name: str) -> str | None:
    """Return an auditable reason when the latest predictions capture is unsafe."""

    rows = conn.execute(
        """
        SELECT capture_complete, stop_reason, round_names_json
        FROM vk_topic_capture_state
        WHERE topic_kind = 'predictions'
        ORDER BY last_captured_at DESC
        """
    ).fetchall()
    for row in rows:
        if bool(row["capture_complete"]):
            continue
        try:
            covered = set(json.loads(str(row["round_names_json"])))
        except json.JSONDecodeError:
            covered = set()
        if not covered or round_name in covered:
            return f"vk_capture:{row['stop_reason']}"
    return None


def record_vk_capture_failure(
    conn: sqlite3.Connection,
    *,
    group_id: int,
    topic_id: int,
    topic_kind: str,
    reason: str,
    notification_chat_ids: Iterable[int] = (),
) -> tuple[VkCaptureState, bool]:
    """Persist a first-page failure so finalization cannot ignore a VK challenge."""

    now = datetime.now().astimezone().isoformat()
    existing = conn.execute(
        """
        SELECT last_complete_at, last_complete_fingerprint
        FROM vk_topic_capture_state
        WHERE group_id = ? AND topic_id = ? AND topic_kind = ?
        """,
        (int(group_id), int(topic_id), topic_kind),
    ).fetchone()
    last_complete_at = str(existing["last_complete_at"]) if existing and existing["last_complete_at"] else None
    last_complete_fingerprint = (
        str(existing["last_complete_fingerprint"])
        if existing and existing["last_complete_fingerprint"]
        else None
    )
    conn.execute(
        """
        INSERT INTO vk_topic_capture_state(
            group_id, topic_id, topic_kind, last_captured_at, capture_complete,
            stop_reason, pages_fetched, submissions_seen, score_line_count,
            round_names_json, earliest_submitted_at, latest_submitted_at,
            warnings_json, content_fingerprint, last_complete_at, last_complete_fingerprint
        )
        VALUES(?, ?, ?, ?, 0, ?, 0, 0, 0, '[]', NULL, NULL, ?, ?, ?, ?)
        ON CONFLICT(group_id, topic_id, topic_kind) DO UPDATE SET
            last_captured_at = excluded.last_captured_at,
            capture_complete = 0,
            stop_reason = excluded.stop_reason,
            pages_fetched = 0,
            submissions_seen = 0,
            score_line_count = 0,
            round_names_json = '[]',
            earliest_submitted_at = NULL,
            latest_submitted_at = NULL,
            warnings_json = excluded.warnings_json,
            content_fingerprint = excluded.content_fingerprint,
            last_complete_at = excluded.last_complete_at,
            last_complete_fingerprint = excluded.last_complete_fingerprint
        """,
        (
            int(group_id),
            int(topic_id),
            topic_kind,
            now,
            reason,
            json.dumps([reason], ensure_ascii=False),
            f"failure:{reason}",
            last_complete_at,
            last_complete_fingerprint,
        ),
    )
    warning_created = enqueue_vk_prediction_notification(
        conn,
        kind="field_incomplete",
        group_id=int(group_id),
        topic_id=int(topic_id),
        source_key=f"capture:unknown:{reason}",
        content_fingerprint="capture-incomplete-v1",
        participant_name="VK capture",
        vk_author="Forecasters Club",
        round_name="unknown",
        payload={"reason": reason, "last_complete_at": last_complete_at or "нет полного снимка"},
        chat_ids=notification_chat_ids,
        created_at=now,
    )
    conn.commit()
    return (
        VkCaptureState(
            group_id=int(group_id),
            topic_id=int(topic_id),
            topic_kind=topic_kind,
            capture_complete=False,
            stop_reason=reason,
            pages_fetched=0,
            round_names=(),
            last_captured_at=now,
            last_complete_at=last_complete_at,
        ),
        warning_created,
    )
