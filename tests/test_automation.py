from datetime import datetime
import unittest

from brucebet.analytics import capture_model_forecasts, model_calibration_summary, round_review
from brucebet.pl_fixtures import import_pl_fixtures, import_pl_results
from brucebet.reminders import due_reminders, mark_delivery_sent, subscribe_chat
from brucebet.storage import connect, reset_db, upsert_match, upsert_match_assessment, upsert_prediction


def completed_fixture(home: str, away: str, home_score: int, away_score: int) -> dict[str, object]:
    return {
        "status": "C",
        "teams": [
            {"team": {"name": home, "club": {"name": home}}},
            {"team": {"name": away, "club": {"name": away}}},
        ],
        "score": {"homeScore": home_score, "awayScore": away_score},
    }


class AutomationTest(unittest.TestCase):
    def test_fixture_import_does_not_score_an_in_progress_match(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        live = completed_fixture("Arsenal", "Chelsea", 1, 0)
        live["status"] = "L"
        live["id"] = 1
        live["gameweek"] = {"gameweek": 1}
        live["kickoff"] = {"millis": 1787230800000}

        import_pl_fixtures(conn, [live])

        self.assertIsNone(conn.execute("SELECT result FROM matches").fetchone()["result"])

    def test_result_sync_accepts_zero_scores_only_after_completed_status(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)

        result = import_pl_results(conn, [completed_fixture("Arsenal", "Chelsea", 0, 1)])

        stored = conn.execute("SELECT result FROM matches").fetchone()
        self.assertEqual(result.finished_seen, 1)
        self.assertEqual(result.updated, 1)
        self.assertEqual(stored["result"], "0:1")

    def test_deadline_deliveries_are_persisted_and_not_duplicated(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        subscribe_chat(conn, 42, now=datetime.fromisoformat("2026-08-14T12:00:00+03:00"))
        now = datetime.fromisoformat("2026-08-15T16:12:00+03:00")

        deliveries = due_reminders(conn, now=now, grace_minutes=35)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].reminder_key, "deadline_minus_20m")
        mark_delivery_sent(conn, deliveries[0].delivery_id, now=now)

        self.assertEqual(due_reminders(conn, now=now, grace_minutes=35), [])

    def test_review_and_calibration_use_frozen_pre_kickoff_forecast(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", "2:1")
        upsert_prediction(conn, "Bruce Wayne", "1", 1, "2:1", "2026-08-15T14:00:00+03:00", "test")
        upsert_prediction(conn, "Igor", "1", 1, "1:0", "2026-08-15T14:00:00+03:00", "test")
        upsert_match_assessment(
            conn,
            {
                "round": "1",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.60",
                "updated_at": "2026-08-14T10:00:00+03:00",
            },
        )

        captured = capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-14T18:00:00+03:00"))
        review = round_review(conn, "1")
        calibration = model_calibration_summary(conn)

        self.assertEqual(captured, 1)
        self.assertTrue(review["complete"])
        self.assertEqual(review["participants"][0]["participant"], "Bruce Wayne")
        self.assertEqual(calibration["exact"], 1)
        self.assertEqual(calibration["points"], 3)


if __name__ == "__main__":
    unittest.main()
