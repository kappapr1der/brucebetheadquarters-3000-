from __future__ import annotations

from datetime import datetime
import unittest

from brucebet.round_summary_notifications import (
    enqueue_round_summary_notification,
    mark_round_summary_delivery_failed,
    mark_round_summary_delivery_sent,
    pending_round_summary_deliveries,
)
from brucebet.storage import (
    active_participant_id,
    connect,
    ensure_participant,
    merge_participants,
    rename_participant,
    reset_db,
    upsert_match,
    upsert_prediction,
)
from brucebet.vk_prediction_import import _registered_participant


class ParticipantMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        reset_db(self.conn)
        upsert_match(self.conn, "1", 1, "Arsenal", "Chelsea", "2030-08-21T20:00:00+03:00", None)

    def tearDown(self) -> None:
        self.conn.close()

    def test_merge_moves_predictions_and_keeps_canonical_payment_status(self) -> None:
        canonical_id = ensure_participant(self.conn, "Сергей", paid=1)
        duplicate_id = ensure_participant(self.conn, "Mr Sam", paid=0)
        upsert_prediction(self.conn, "Mr Sam", "1", 1, "2:1", "2030-08-21T15:00:00+03:00", "test")

        merged_id = merge_participants(self.conn, "Сергей", "Mr Sam")
        self.conn.commit()

        self.assertEqual(merged_id, canonical_id)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM participants WHERE id = ?", (duplicate_id,)).fetchone())
        prediction = self.conn.execute("SELECT participant_id FROM predictions").fetchone()
        self.assertEqual(int(prediction["participant_id"]), canonical_id)
        season = self.conn.execute(
            "SELECT paid, active, alias FROM season_participants WHERE participant_id = ?",
            (canonical_id,),
        ).fetchone()
        self.assertEqual((season["paid"], season["active"], season["alias"]), (1, 1, "Mr Sam"))

    def test_merge_rejects_overlapping_predictions(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        ensure_participant(self.conn, "Mr Sam", paid=0)
        upsert_prediction(self.conn, "Сергей", "1", 1, "2:1", "2030-08-21T15:00:00+03:00", "test")
        upsert_prediction(self.conn, "Mr Sam", "1", 1, "1:0", "2030-08-21T15:00:00+03:00", "test")

        with self.assertRaisesRegex(ValueError, "same match"):
            merge_participants(self.conn, "Сергей", "Mr Sam")

    def test_registered_vk_author_resolves_to_canonical_participant(self) -> None:
        participant_id = ensure_participant(self.conn, "Сергей", paid=1)
        timestamp = datetime.now().astimezone().isoformat()
        self.conn.execute(
            """
            INSERT INTO vk_registration_entries(
                group_id, topic_id, source_key, participant_id, vk_author, participant_name,
                submitted_at, fee_intent, fee_amount_rub, payment_status,
                first_seen_at, last_seen_at, notification_status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 2, "registration:mr-sam", participant_id, "Mr Sam", "Сергей", timestamp, "paid_declared", 300, "confirmed", timestamp, timestamp, "sent"),
        )

        self.assertEqual(_registered_participant(self.conn, "Mr Sam"), "Сергей")

    def test_renamed_participant_keeps_alias_for_vk_and_imports(self) -> None:
        participant_id = ensure_participant(self.conn, "Сергей", paid=1)

        renamed_id = rename_participant(self.conn, "Сергей", "Mr Sam", alias="Сергей")
        resolved_id = ensure_participant(self.conn, "Сергей", paid=None)

        self.assertEqual((renamed_id, resolved_id), (participant_id, participant_id))
        self.assertEqual(active_participant_id(self.conn, "Mr Sam"), participant_id)
        self.assertEqual(active_participant_id(self.conn, "Сергей"), participant_id)
        self.assertEqual(_registered_participant(self.conn, "Сергей"), "Mr Sam")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM participants WHERE name = 'Сергей'").fetchone())


class RoundSummaryOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        reset_db(self.conn)
        upsert_match(self.conn, "1", 1, "Arsenal", "Chelsea", "2030-08-21T20:00:00+03:00", None)

    def tearDown(self) -> None:
        self.conn.close()

    def test_round_summary_is_queued_once_and_retried_until_sent(self) -> None:
        created = enqueue_round_summary_notification(
            self.conn,
            season_id=1,
            round_id=1,
            text="Итоги тура 1",
            chat_ids=(42,),
            created_at="2030-08-25T20:00:00+03:00",
        )
        duplicate = enqueue_round_summary_notification(
            self.conn,
            season_id=1,
            round_id=1,
            text="Итоги тура 1",
            chat_ids=(42,),
            created_at="2030-08-25T20:00:00+03:00",
        )
        self.conn.commit()

        self.assertTrue(created)
        self.assertFalse(duplicate)
        delivery = pending_round_summary_deliveries(self.conn, (42,))[0]
        mark_round_summary_delivery_failed(self.conn, delivery, "network", attempted_at="2030-08-25T20:01:00+03:00")
        retry = pending_round_summary_deliveries(self.conn, (42,))[0]
        mark_round_summary_delivery_sent(self.conn, retry, sent_at="2030-08-25T20:02:00+03:00")

        self.assertEqual(pending_round_summary_deliveries(self.conn, (42,)), [])
