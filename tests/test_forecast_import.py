from datetime import datetime
import unittest

from brucebet.forecast_import import (
    ExpectedMatch,
    import_forecast_block,
    import_participant_block,
    parse_forecast_block,
    parse_participant_block,
)
from brucebet.storage import connect, ensure_participant, init_db, reset_db, upsert_match, upsert_prediction


MATCHES = [
    ExpectedMatch(1, "Arsenal", "Chelsea"),
    ExpectedMatch(2, "Liverpool", "Burnley"),
    ExpectedMatch(3, "Brighton", "Newcastle"),
]


class ForecastImportTest(unittest.TestCase):
    def test_participant_list_tracks_fee_markers_and_duplicates(self) -> None:
        report = parse_participant_block(
            "Игорь Григорьев - 300р\nСтас Ручкин без взноса\nАнна Бухтеева.\nМихаил Макаров. Взнос 300 рублей\nИгорь Григорьев +"
        )

        self.assertEqual(
            [(item.name, item.paid) for item in report.entries],
            [("Игорь Григорьев", True), ("Стас Ручкин", False), ("Анна Бухтеева", None), ("Михаил Макаров", True)],
        )
        self.assertEqual(report.duplicate_names, ("Игорь Григорьев",))

    def test_unspecified_new_participant_is_not_added_to_the_prize_bank(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)

        report = import_participant_block(conn, "Анна Бухтеева")
        row = conn.execute("SELECT paid FROM participants WHERE name = 'Анна Бухтеева'").fetchone()

        self.assertEqual(report.unspecified_count, 1)
        self.assertEqual(row["paid"], 0)

    def test_mixed_labelled_and_ordered_scores_are_normalized(self) -> None:
        report = parse_forecast_block(
            "Arsenal - Chelsea 2 - 1\n0;0\n3 : 1",
            MATCHES,
        )

        self.assertEqual([(item.position, item.score) for item in report.forecasts], [(1, "2:1"), (2, "0:0"), (3, "3:1")])
        self.assertEqual(len(report.normalized), 3)
        self.assertEqual(report.missing_positions, ())
        self.assertEqual(report.invalid_lines, ())

    def test_ambiguous_invalid_and_extra_scores_are_reported_without_guessing(self) -> None:
        report = parse_forecast_block(
            "2:1\n10:0\n1:0 / 1:1\n0:0\n4:0",
            MATCHES[:2],
        )

        self.assertEqual([(item.position, item.score) for item in report.forecasts], [(1, "2:1"), (2, "0:0")])
        self.assertEqual(len(report.invalid_lines), 2)
        self.assertEqual(len(report.extra_lines), 1)
        self.assertEqual(report.missing_positions, ())

    def test_import_saves_only_accepted_positions_with_message_timestamp(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match(conn, "1", 2, "Liverpool", "Burnley", "2026-08-15T20:30:00+03:00", None)

        report = import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="2 - 1\n10:0",
            submitted_at=datetime.fromisoformat("2026-08-15T14:00:00+03:00"),
            source="test",
        )
        rows = list(conn.execute("SELECT score, submitted_at, source FROM predictions ORDER BY match_id"))

        self.assertEqual(report.accepted_count, 1)
        self.assertEqual(report.missing_positions, (2,))
        self.assertEqual([(row["score"], row["submitted_at"], row["source"]) for row in rows], [("2:1", "2026-08-15T14:00:00+03:00", "test:line-1")])

    def test_late_import_cannot_replace_an_existing_forecast(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match(conn, "1", 2, "Liverpool", "Burnley", "2026-08-15T20:30:00+03:00", None)
        import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="2:1\n1:0",
            submitted_at=datetime.fromisoformat("2026-08-15T14:00:00+03:00"),
            source="test",
        )

        report = import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="3:0",
            submitted_at=datetime.fromisoformat("2026-08-15T16:31:00+03:00"),
            source="test",
        )
        rows = list(conn.execute("SELECT score FROM predictions ORDER BY match_id"))

        self.assertEqual(report.stored_count, 0)
        self.assertEqual(report.protected_positions, (1,))
        self.assertEqual([row["score"] for row in rows], ["2:1", "1:0"])

        revisions = list(
            conn.execute(
                "SELECT normalized_score, eligibility_decision, reason, projected FROM prediction_revisions WHERE match_id = 1 ORDER BY id"
            )
        )
        self.assertEqual(
            [(row["normalized_score"], row["eligibility_decision"], row["reason"], row["projected"]) for row in revisions],
            [("2:1", "accepted", "before_round_deadline", 1), ("3:0", "rejected", "late_edit", 0)],
        )

    def test_duplicate_source_item_is_revision_noop(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        kwargs = dict(
            participant="Igor",
            round_name="1",
            text="2:1",
            submitted_at="2026-08-15T14:00:00+03:00",
            source="telegram-forecast",
            source_item_id="telegram:42:100",
        )

        first = import_forecast_block(conn, **kwargs)
        second = import_forecast_block(conn, **kwargs)

        self.assertEqual(first.stored_positions, (1,))
        self.assertEqual(second.stored_positions, (1,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT score FROM predictions").fetchone()[0], "2:1")

    def test_pre_deadline_edit_appends_revision_and_updates_projection(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        common = dict(
            participant="Igor",
            round_name="1",
            source="telegram-forecast",
            source_item_id="telegram:42:100",
        )
        import_forecast_block(conn, text="2:1", submitted_at="2026-08-15T14:00:00+03:00", **common)
        import_forecast_block(conn, text="1:1", submitted_at="2026-08-15T14:30:00+03:00", **common)

        rows = list(
            conn.execute(
                "SELECT normalized_score, previous_revision_id, eligibility_decision FROM prediction_revisions ORDER BY id"
            )
        )
        self.assertEqual(conn.execute("SELECT score FROM predictions").fetchone()[0], "1:1")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["previous_revision_id"], 1)
        self.assertEqual(rows[1]["eligibility_decision"], "accepted")

    def test_missing_and_naive_timestamps_are_quarantined(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)

        missing = import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="2:1",
            submitted_at=None,
            source="external",
            source_item_id="external:missing",
        )
        naive = import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="2:1",
            submitted_at="2026-08-15T14:00:00",
            source="external",
            source_item_id="external:naive",
        )

        self.assertEqual(missing.quarantined_positions, (1,))
        self.assertEqual(naive.quarantined_positions, (1,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
        self.assertEqual(
            [row[0] for row in conn.execute("SELECT reason FROM prediction_revisions ORDER BY id")],
            ["missing_submitted_at", "naive_submitted_at"],
        )

    def test_first_partial_late_forecast_uses_match_cutoff(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_participant(conn, "Igor", paid=1)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match(conn, "1", 2, "Liverpool", "Burnley", "2026-08-15T20:30:00+03:00", None)

        report = import_forecast_block(
            conn,
            participant="Igor",
            round_name="1",
            text="Liverpool - Burnley 1:0",
            submitted_at="2026-08-15T18:30:00+03:00",
            source="telegram-forecast",
            source_item_id="telegram:42:101",
        )
        revision = conn.execute(
            "SELECT eligibility_decision, reason, deadline_at FROM prediction_revisions"
        ).fetchone()

        self.assertEqual(report.stored_positions, (2,))
        self.assertEqual(revision["eligibility_decision"], "accepted_partial_late")
        self.assertEqual(revision["reason"], "before_match_deadline")
        self.assertEqual(revision["deadline_at"], "2026-08-15T19:00:00+03:00")


    def test_unknown_or_invalid_forecasts_never_enroll_a_participant(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)

        for text in ("2:1", "10:0", "нечитаемый блок"):
            with self.assertRaisesRegex(ValueError, "не зарегистрирован"):
                import_forecast_block(
                    conn,
                    participant="Ghost Applicant",
                    round_name="1",
                    text=text,
                    submitted_at=datetime.fromisoformat("2026-08-15T14:00:00+03:00"),
                    source="test",
                )

        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM participants").fetchone()["count"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM season_participants").fetchone()["count"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM predictions").fetchone()["count"], 0)


    def test_init_db_backfills_a_legacy_prediction_once(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        ensure_participant(conn, "Igor", paid=1)
        upsert_prediction(
            conn,
            participant="Igor",
            round_name="1",
            position=1,
            score="2:1",
            submitted_at="2026-08-15T14:00:00+03:00",
            source="legacy-import",
        )
        conn.execute("DELETE FROM prediction_revisions")
        conn.commit()

        init_db(conn)
        first = list(
            conn.execute(
                "SELECT normalized_score, eligibility_decision, projected FROM prediction_revisions ORDER BY id"
            )
        )
        init_db(conn)
        second_count = conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0]

        self.assertEqual(
            [(row["normalized_score"], row["eligibility_decision"], row["projected"]) for row in first],
            [("2:1", "accepted", 1)],
        )
        self.assertEqual(second_count, 1)

if __name__ == "__main__":
    unittest.main()
