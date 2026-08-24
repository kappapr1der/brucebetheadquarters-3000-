from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Iterable


@dataclass(frozen=True)
class RoundSummaryNotificationDelivery:
    event_key: str
    chat_id: int
    text: str


def enqueue_round_summary_notification(
    conn: sqlite3.Connection,
    *,
    season_id: int,
    round_id: int,
    text: str,
    chat_ids: Iterable[int],
    created_at: str,
) -> bool:
    """Queue one immutable contest table per completed round."""

    event_key = f"round-summary:{season_id}:{round_id}"
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO round_summary_notifications(event_key, season_id, round_id, text, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (event_key, season_id, round_id, text, created_at),
    )
    if cursor.rowcount == 0:
        return False
    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    conn.executemany(
        """
        INSERT INTO round_summary_notification_deliveries(event_key, chat_id, status, created_at)
        VALUES(?, ?, 'pending', ?)
        """,
        [(event_key, chat_id, created_at) for chat_id in recipients],
    )
    return True


def pending_round_summary_deliveries(
    conn: sqlite3.Connection,
    chat_ids: Iterable[int],
) -> list[RoundSummaryNotificationDelivery]:
    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    if not recipients:
        return []
    placeholders = ", ".join("?" for _ in recipients)
    rows = conn.execute(
        f"""
        SELECT delivery.event_key, delivery.chat_id, event.text
        FROM round_summary_notification_deliveries delivery
        JOIN round_summary_notifications event ON event.event_key = delivery.event_key
        WHERE delivery.status = 'pending' AND delivery.chat_id IN ({placeholders})
        ORDER BY event.created_at, event.event_key, delivery.chat_id
        """,
        recipients,
    ).fetchall()
    return [
        RoundSummaryNotificationDelivery(
            event_key=str(row["event_key"]),
            chat_id=int(row["chat_id"]),
            text=str(row["text"]),
        )
        for row in rows
    ]


def mark_round_summary_delivery_sent(
    conn: sqlite3.Connection,
    delivery: RoundSummaryNotificationDelivery,
    *,
    sent_at: str,
) -> None:
    conn.execute(
        """
        UPDATE round_summary_notification_deliveries
        SET status = 'sent', sent_at = ?, error = NULL, last_attempt_at = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (sent_at, sent_at, delivery.event_key, delivery.chat_id),
    )
    conn.commit()


def mark_round_summary_delivery_failed(
    conn: sqlite3.Connection,
    delivery: RoundSummaryNotificationDelivery,
    error: str,
    *,
    attempted_at: str,
) -> None:
    conn.execute(
        """
        UPDATE round_summary_notification_deliveries
        SET attempts = attempts + 1, last_attempt_at = ?, error = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (attempted_at, error[:500], delivery.event_key, delivery.chat_id),
    )
    conn.commit()
