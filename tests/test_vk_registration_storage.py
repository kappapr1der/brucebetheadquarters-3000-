from __future__ import annotations

from datetime import datetime
import unittest

from brucebet.storage import (
    active_season_id,
    connect,
    init_db,
    mark_vk_registration_alert_sent,
    record_vk_registration_entries,
)
from brucebet.vk_dry_run import VkRegistrationEntry
from brucebet.vk_parser import MSK


def entry(
    source_key: str,
    participant: str,
    fee_intent: str,
    amount: int | None,
) -> VkRegistrationEntry:
    return VkRegistrationEntry(
        source_key=source_key,
        vk_author=participant,
        participant=participant,
        submitted_at=datetime(2026, 8, 10, 19, 0, tzinfo=MSK),
        fee_intent=fee_intent,  # type: ignore[arg-type]
        fee_amount_rub=amount,
        payment_status="confirmed" if fee_intent == "paid_declared" else "not_applicable",
        source_line=1,
    )


class VkRegistrationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_entries_create_paid_and_free_season_participants_once(self) -> None:
        entries = [
            entry("vk:1", "Сергей", "paid_declared", 500),
            entry("vk:2", "Георгий Карев", "free", None),
        ]
        alerts = record_vk_registration_entries(
            self.conn,
            217130885,
            67251857,
            entries,
            seen_at="2026-08-10T19:30:00+03:00",
        )

        self.assertEqual([(item.participant_name, item.fee_amount_rub) for item in alerts], [("Сергей", 500), ("Георгий Карев", None)])
        season_id = active_season_id(self.conn)
        rows = self.conn.execute(
            """
            SELECT participants.name, season_participants.paid
            FROM season_participants
            JOIN participants ON participants.id = season_participants.participant_id
            WHERE season_participants.season_id = ?
            ORDER BY participants.name
            """,
            (season_id,),
        ).fetchall()
        self.assertEqual([(row["name"], row["paid"]) for row in rows], [("Георгий Карев", 0), ("Сергей", 1)])

        for alert in alerts:
            mark_vk_registration_alert_sent(self.conn, alert, sent_at="2026-08-10T19:31:00+03:00")
        repeated = record_vk_registration_entries(
            self.conn,
            217130885,
            67251857,
            entries,
            seen_at="2026-08-10T19:35:00+03:00",
        )
        self.assertEqual(repeated, [])


if __name__ == "__main__":
    unittest.main()
