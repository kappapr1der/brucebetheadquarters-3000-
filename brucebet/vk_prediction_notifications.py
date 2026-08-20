from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Iterable, Mapping


MAX_NOTIFICATION_TEXT = 3900


@dataclass(frozen=True)
class VkPredictionNotificationDelivery:
    event_key: str
    chat_id: int
    kind: str
    participant_name: str
    round_name: str
    payload: Mapping[str, object]


def prediction_notification_key(
    *,
    kind: str,
    group_id: int,
    topic_id: int,
    source_key: str,
    content_fingerprint: str,
) -> str:
    """Return an immutable source-derived key, never a VK list position."""

    return ":".join(
        (
            "vk-prediction-notification",
            kind,
            str(int(group_id)),
            str(int(topic_id)),
            source_key,
            content_fingerprint,
        )
    )


def enqueue_vk_prediction_notification(
    conn: sqlite3.Connection,
    *,
    kind: str,
    group_id: int,
    topic_id: int,
    source_key: str,
    content_fingerprint: str,
    participant_name: str,
    vk_author: str,
    round_name: str,
    payload: Mapping[str, object],
    chat_ids: Iterable[int],
    created_at: str,
) -> bool:
    """Persist an event and its intended recipient deliveries atomically.

    The caller deliberately owns the surrounding import transaction.  A
    duplicate capture therefore cannot create a second event or a second
    delivery row for a chat.
    """

    event_key = prediction_notification_key(
        kind=kind,
        group_id=group_id,
        topic_id=topic_id,
        source_key=source_key,
        content_fingerprint=content_fingerprint,
    )
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO vk_prediction_notifications(
            event_key, group_id, topic_id, kind, source_key, content_fingerprint,
            participant_name, vk_author, round_name, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            int(group_id),
            int(topic_id),
            kind,
            source_key,
            content_fingerprint,
            participant_name,
            vk_author,
            round_name,
            payload_json,
            created_at,
        ),
    )
    if cursor.rowcount == 0:
        return False

    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    conn.executemany(
        """
        INSERT INTO vk_prediction_notification_deliveries(event_key, chat_id, status, created_at)
        VALUES(?, ?, 'pending', ?)
        """,
        [(event_key, chat_id, created_at) for chat_id in recipients],
    )
    return True


def pending_vk_prediction_notification_deliveries(
    conn: sqlite3.Connection,
    chat_ids: Iterable[int],
) -> list[VkPredictionNotificationDelivery]:
    """Return only pending deliveries still addressed to the active whitelist."""

    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    if not recipients:
        return []
    placeholders = ", ".join("?" for _ in recipients)
    rows = conn.execute(
        f"""
        SELECT
            delivery.event_key,
            delivery.chat_id,
            event.kind,
            event.participant_name,
            event.round_name,
            event.payload_json
        FROM vk_prediction_notification_deliveries delivery
        JOIN vk_prediction_notifications event ON event.event_key = delivery.event_key
        WHERE delivery.status = 'pending'
          AND delivery.chat_id IN ({placeholders})
        ORDER BY event.created_at, event.event_key, delivery.chat_id
        """,
        recipients,
    ).fetchall()
    return [
        VkPredictionNotificationDelivery(
            event_key=str(row["event_key"]),
            chat_id=int(row["chat_id"]),
            kind=str(row["kind"]),
            participant_name=str(row["participant_name"]),
            round_name=str(row["round_name"]),
            payload=json.loads(str(row["payload_json"])),
        )
        for row in rows
    ]


def mark_vk_prediction_notification_delivery_sent(
    conn: sqlite3.Connection,
    delivery: VkPredictionNotificationDelivery,
    *,
    sent_at: str,
) -> None:
    conn.execute(
        """
        UPDATE vk_prediction_notification_deliveries
        SET status = 'sent', sent_at = ?, error = NULL, last_attempt_at = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (sent_at, sent_at, delivery.event_key, delivery.chat_id),
    )
    conn.commit()


def mark_vk_prediction_notification_delivery_failed(
    conn: sqlite3.Connection,
    delivery: VkPredictionNotificationDelivery,
    error: str,
    *,
    attempted_at: str,
) -> None:
    conn.execute(
        """
        UPDATE vk_prediction_notification_deliveries
        SET status = 'pending', attempts = attempts + 1, last_attempt_at = ?, error = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (attempted_at, error[:500], delivery.event_key, delivery.chat_id),
    )
    conn.commit()


def _round_label(round_name: str) -> str:
    compact = " ".join(round_name.split())
    return compact if compact.casefold().startswith("тур ") else f"Тур {compact}"


def _deadline_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "не определён"
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return value
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return value
    offset_seconds = int(deadline.utcoffset().total_seconds())
    sign = "+" if offset_seconds >= 0 else "-"
    hours, remainder = divmod(abs(offset_seconds), 3600)
    minutes = remainder // 60
    offset = f"{sign}{hours:02d}" if minutes == 0 else f"{sign}{hours:02d}:{minutes:02d}"
    return f"до {deadline:%H:%M} {offset}"


def _change_lines(payload: Mapping[str, object], *, include_current: bool = False) -> list[str]:
    changes = payload.get("changes")
    if not isinstance(changes, list):
        return []
    lines: list[str] = []
    for item in changes:
        if not isinstance(item, Mapping):
            continue
        home = str(item.get("home", "?")).strip() or "?"
        away = str(item.get("away", "?")).strip() or "?"
        old_score = str(item.get("old_score", "?")).strip() or "?"
        new_score = str(item.get("new_score", "?")).strip() or "?"
        line = f"{home} — {away}: {old_score} → {new_score}"
        if include_current:
            current_score = str(item.get("current_score", old_score)).strip() or old_score
            line += f" (текущий: {current_score})"
        lines.append(line)
    return lines


def render_vk_prediction_notification(delivery: VkPredictionNotificationDelivery) -> str:
    """Render a compact Russian admin alert from immutable outbox payload."""

    payload = delivery.payload
    round_label = _round_label(delivery.round_name)
    participant = delivery.participant_name or "unknown"
    if delivery.kind == "new":
        accepted = int(payload.get("accepted", 0))
        expected = int(payload.get("expected", accepted))
        lines = [
            f"🎯 Новый прогноз — {round_label}",
            participant,
            f"Принято: {accepted}/{expected}",
            "Источник: VK",
            f"Дедлайн: {_deadline_label(payload.get('deadline_at'))} ✅",
        ]
    elif delivery.kind == "edit":
        lines = [f"✏️ Изменён прогноз — {round_label}", participant, *_change_lines(payload)]
    elif delivery.kind == "late_edit":
        lines = [
            "⛔ Поздняя правка отклонена",
            f"{participant} — {round_label}",
            *_change_lines(payload, include_current=True),
            "Текущий прогноз сохранён без изменений.",
            f"Причина: {payload.get('reason', 'late_edit')}",
        ]
    elif delivery.kind == "late_submission":
        lines = [
            "⛔ Поздний прогноз отклонён",
            f"{participant} — {round_label}",
            *_change_lines(payload, include_current=True),
            "Текущий прогноз не изменён.",
            f"Причина: {payload.get('reason', 'late_submission')}",
        ]
    else:
        lines = [
            "⚠️ Прогноз отправлен на проверку",
            f"Участник: {participant}",
            f"Тур: {delivery.round_name or 'unknown'}",
            f"Причина: {payload.get('reason', 'unknown')}",
        ]
    text = "\n".join(line for line in lines if line)
    return text[:MAX_NOTIFICATION_TEXT]
