from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3

from .analytics import round_deadlines
from .service_messages import deadline_after_message, deadline_reminder_schedule


DEFAULT_REMINDER_GRACE_MINUTES = 35


@dataclass(frozen=True)
class ReminderDelivery:
    delivery_id: int
    chat_id: int
    round_id: int
    round_name: str
    reminder_key: str
    scheduled_at: datetime
    text: str


def _now_iso(now: datetime) -> str:
    return now.astimezone().isoformat()


def subscribe_chat(conn: sqlite3.Connection, chat_id: int, now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    value = _now_iso(now)
    conn.execute(
        """
        INSERT INTO reminder_subscriptions(chat_id, enabled, created_at, updated_at)
        VALUES(?, 1, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET enabled = 1, updated_at = excluded.updated_at
        """,
        (chat_id, value, value),
    )
    conn.commit()


def active_subscriptions(conn: sqlite3.Connection) -> list[int]:
    return [
        int(row["chat_id"])
        for row in conn.execute("SELECT chat_id FROM reminder_subscriptions WHERE enabled = 1 ORDER BY chat_id")
    ]


def reminder_overview(conn: sqlite3.Connection, chat_id: int, now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now().astimezone()
    subscription = conn.execute(
        "SELECT enabled, created_at, updated_at FROM reminder_subscriptions WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    due = due_reminders(conn, chat_ids=[chat_id], now=now, persist=False)
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM reminder_deliveries
        WHERE chat_id = ?
        GROUP BY status
        """,
        (chat_id,),
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "subscribed": bool(subscription and subscription["enabled"]),
        "due_now": len(due),
        "sent": counts.get("sent", 0),
        "pending": counts.get("pending", 0),
        "next_at": min((item.scheduled_at for item in due), default=None),
    }


def _round_ids(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id, name FROM rounds WHERE season_id = (SELECT id FROM seasons WHERE active = 1 ORDER BY id DESC LIMIT 1)"
    ).fetchall()
    return {str(row["name"]): int(row["id"]) for row in rows}


def due_reminders(
    conn: sqlite3.Connection,
    chat_ids: list[int] | None = None,
    now: datetime | None = None,
    lock_minutes: int = 90,
    grace_minutes: int = DEFAULT_REMINDER_GRACE_MINUTES,
    persist: bool = True,
) -> list[ReminderDelivery]:
    """Return unsent reminders in a small grace window and optionally persist them.

    The delivery row is deliberately written before Telegram is called. A failed
    request remains pending and is retried on the next scheduler tick; a sent row
    is never sent twice after a restart.
    """
    now = now or datetime.now().astimezone()
    ids = chat_ids if chat_ids is not None else active_subscriptions(conn)
    if not ids:
        return []
    round_ids = _round_ids(conn)
    grace = timedelta(minutes=max(1, grace_minutes))
    deliveries: list[ReminderDelivery] = []

    for deadline in round_deadlines(conn, lock_minutes=lock_minutes):
        effective = deadline.effective_deadline_at
        round_id = round_ids.get(deadline.round_name)
        if effective is None or round_id is None:
            continue
        plans = [(item.key, item.send_at, item.reply.text) for item in deadline_reminder_schedule(effective)]
        plans.append(("deadline_passed", effective, deadline_after_message(effective).text))
        for reminder_key, scheduled_at, text in plans:
            if scheduled_at > now or now - scheduled_at > grace:
                continue
            if reminder_key != "deadline_passed" and now > effective:
                continue
            for chat_id in ids:
                existing = conn.execute(
                    """
                    SELECT id, status FROM reminder_deliveries
                    WHERE chat_id = ? AND round_id = ? AND reminder_key = ?
                    """,
                    (chat_id, round_id, reminder_key),
                ).fetchone()
                if existing and existing["status"] == "sent":
                    continue
                if persist:
                    if existing:
                        conn.execute(
                            """
                            UPDATE reminder_deliveries
                            SET scheduled_at = ?, text = ?, status = 'pending', error = NULL
                            WHERE id = ?
                            """,
                            (_now_iso(scheduled_at), text, int(existing["id"])),
                        )
                        delivery_id = int(existing["id"])
                    else:
                        cursor = conn.execute(
                            """
                            INSERT INTO reminder_deliveries(chat_id, round_id, reminder_key, scheduled_at, text, status)
                            VALUES(?, ?, ?, ?, ?, 'pending')
                            """,
                            (chat_id, round_id, reminder_key, _now_iso(scheduled_at), text),
                        )
                        delivery_id = int(cursor.lastrowid)
                else:
                    delivery_id = int(existing["id"]) if existing else 0
                deliveries.append(
                    ReminderDelivery(
                        delivery_id=delivery_id,
                        chat_id=chat_id,
                        round_id=round_id,
                        round_name=deadline.round_name,
                        reminder_key=reminder_key,
                        scheduled_at=scheduled_at,
                        text=text,
                    )
                )
    if persist:
        conn.commit()
    return deliveries


def mark_delivery_sent(conn: sqlite3.Connection, delivery_id: int, now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    conn.execute(
        "UPDATE reminder_deliveries SET status = 'sent', sent_at = ?, error = NULL WHERE id = ?",
        (_now_iso(now), delivery_id),
    )
    conn.commit()


def mark_delivery_failed(conn: sqlite3.Connection, delivery_id: int, error: str) -> None:
    conn.execute(
        "UPDATE reminder_deliveries SET status = 'pending', error = ? WHERE id = ?",
        (error[:500], delivery_id),
    )
    conn.commit()
