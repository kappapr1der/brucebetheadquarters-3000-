from datetime import datetime
import unittest

from brucebet.forecast_import import (
    ExpectedMatch,
    import_forecast_block,
    import_participant_block,
    parse_forecast_block,
    parse_participant_block,
)
from brucebet.storage import connect, reset_db, upsert_match


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


if __name__ == "__main__":
    unittest.main()
