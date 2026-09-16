from __future__ import annotations

from datetime import datetime
import unittest

from brucebet.storage import (
    active_season_id,
    connect,
    ensure_participant,
    ensure_round,
    ensure_season_participant,
    reset_db,
)


class StorageWriteGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        reset_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_season_participant_skips_equal_state_but_persists_real_changes(self) -> None:
        participant_id = ensure_participant(self.conn, "Physical No-op", paid=1)
        self.conn.commit()

        before_noop = self.conn.total_changes
        ensure_season_participant(self.conn, participant_id, paid=1, active=1)
        self.assertEqual(self.conn.total_changes - before_noop, 0)

        before_change = self.conn.total_changes
        ensure_season_participant(self.conn, participant_id, paid=0, active=0)
        self.assertEqual(self.conn.total_changes - before_change, 1)
        row = self.conn.execute(
            "SELECT paid, active FROM season_participants WHERE season_id = ? AND participant_id = ?",
            (active_season_id(self.conn), participant_id),
        ).fetchone()
        self.assertEqual((row["paid"], row["active"]), (0, 0))

    def test_round_skips_equal_state_but_persists_real_deadline_change(self) -> None:
        first_deadline = "2030-08-21T20:30:00+03:00"
        second_deadline = "2030-08-21T21:00:00+03:00"
        round_id = ensure_round(self.conn, "7", first_deadline)
        self.conn.commit()

        before_noop = self.conn.total_changes
        self.assertEqual(ensure_round(self.conn, "7"), round_id)
        self.assertEqual(ensure_round(self.conn, "7", first_deadline), round_id)
        self.assertEqual(self.conn.total_changes - before_noop, 0)

        before_change = self.conn.total_changes
        self.assertEqual(ensure_round(self.conn, "7", second_deadline), round_id)
        self.assertEqual(self.conn.total_changes - before_change, 1)
        row = self.conn.execute(
            "SELECT sort_order, deadline_at FROM rounds WHERE id = ?",
            (round_id,),
        ).fetchone()
        self.assertEqual(row["sort_order"], 7)
        self.assertEqual(datetime.fromisoformat(row["deadline_at"]), datetime.fromisoformat(second_deadline))


if __name__ == "__main__":
    unittest.main()
