from datetime import datetime
import unittest

from brucebet.analytics import capture_model_forecasts, missing_forecasts_summary, model_calibration_summary, ready_summary, round_review
from brucebet.pl_fixtures import import_pl_fixtures, import_pl_results
from brucebet.rehearsal import run_rehearsal
from brucebet.reminders import due_reminders, mark_delivery_sent, subscribe_chat
from brucebet.storage import (
    connect,
    mark_premature_model_forecasts,
    manual_result_history,
    manual_prediction_history,
    reset_db,
    set_manual_prediction_override,
    set_manual_match_result,
    upsert_match,
    upsert_match_assessment,
    upsert_prediction,
)


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

        captured = capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:31:00+03:00"))
        review = round_review(conn, "1")
        calibration = model_calibration_summary(conn)

        self.assertEqual(captured, 1)
        self.assertTrue(review["complete"])
        self.assertEqual(review["participants"][0]["participant"], "Bruce Wayne")
        self.assertEqual(calibration["exact"], 1)
        self.assertEqual(calibration["points"], 3)

    def test_ready_summary_exposes_missing_field_and_data_gaps(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_prediction(conn, "Bruce Wayne", "1", 1, "2:1", "2026-08-15T14:00:00+03:00", "test")
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
        capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:31:00+03:00"))

        item = ready_summary(
            conn,
            now=datetime.fromisoformat("2026-08-15T15:00:00+03:00"),
        )

        self.assertEqual(item["round_name"], "1")
        self.assertEqual(item["your_predictions"], 1)
        self.assertEqual(item["model_forecasts"], 1)
        self.assertEqual(item["status"], "attention")
        self.assertTrue(any("FPL" in row for row in item["warnings"]))

    def test_model_forecasts_freeze_only_the_due_complete_round(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match(conn, "2", 1, "Liverpool", "Everton", "2026-08-22T18:00:00+03:00", None)
        for round_name in ("1", "2"):
            upsert_match_assessment(
                conn,
                {
                    "round": round_name,
                    "position": "1",
                    "suggested_score": "2:1",
                    "risk_level": "medium",
                    "confidence": "0.60",
                    "updated_at": "2026-08-15T10:00:00+03:00",
                },
            )

        with self.assertRaisesRegex(ValueError, "aware timestamp"):
            capture_model_forecasts(conn, now=datetime(2026, 8, 15, 16, 31))
        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:29:00+03:00")), 0)
        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:31:00+03:00")), 1)
        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:32:00+03:00")), 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) AS count FROM model_forecasts WHERE legacy_premature = 0").fetchone()["count"],
            1,
        )
        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-22T18:01:00+03:00")), 0)

    def test_zero_lock_uses_final_pick_lead_for_model_snapshot(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match_assessment(
            conn,
            {
                "round": "1",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.60",
                "updated_at": "2026-08-15T10:00:00+03:00",
            },
        )

        self.assertEqual(
            capture_model_forecasts(
                conn,
                now=datetime.fromisoformat("2026-08-15T17:49:00+03:00"),
                lock_minutes=0,
                capture_lead_minutes=10,
            ),
            0,
        )
        self.assertEqual(
            capture_model_forecasts(
                conn,
                now=datetime.fromisoformat("2026-08-15T17:50:00+03:00"),
                lock_minutes=0,
                capture_lead_minutes=10,
            ),
            1,
        )
        row = conn.execute("SELECT freeze_reason, legacy_premature FROM model_forecasts").fetchone()
        self.assertEqual((row["freeze_reason"], row["legacy_premature"]), ("pre_deadline_final", 0))
        self.assertEqual(mark_premature_model_forecasts(conn), 0)

    def test_premature_legacy_model_forecast_is_archived_before_replacement(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        match_id = upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match_assessment(
            conn,
            {
                "round": "1",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.60",
                "updated_at": "2026-08-15T10:00:00+03:00",
            },
        )
        conn.execute(
            """
            INSERT INTO model_forecasts(match_id, model_key, suggested_score, confidence, risk_level, captured_at)
            VALUES(?, 'brucebet', '1:0', 0.40, 'low', '2026-08-14T10:00:00+03:00')
            """,
            (match_id,),
        )
        conn.commit()

        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:31:00+03:00")), 1)
        current = conn.execute("SELECT suggested_score, legacy_premature FROM model_forecasts").fetchone()
        archived = conn.execute("SELECT suggested_score, reason FROM model_forecast_legacy_audit").fetchone()

        self.assertEqual((current["suggested_score"], current["legacy_premature"]), ("2:1", 0))
        self.assertEqual((archived["suggested_score"], archived["reason"]), ("1:0", "replaced_after_premature_freeze"))

    def test_model_forecast_freeze_skips_rounds_with_missing_kickoff(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", None, None)
        upsert_match_assessment(
            conn,
            {
                "round": "1",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.60",
                "updated_at": "2026-08-15T10:00:00+03:00",
            },
        )

        self.assertEqual(capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-15T16:31:00+03:00")), 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM model_forecasts").fetchone()["count"], 0)

    def test_manual_result_override_keeps_audit_history(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        match_id = upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", "1:1")

        previous, current = set_manual_match_result(
            conn,
            match_id,
            "2-1",
            actor_chat_id=42,
            reason="official feed delay",
            changed_at="2026-08-15T20:00:00+03:00",
        )
        history = manual_result_history(conn, match_id)

        self.assertEqual(previous, "1:1")
        self.assertEqual(current, "2:1")
        self.assertEqual(history[0]["previous_result"], "1:1")
        self.assertEqual(history[0]["new_result"], "2:1")
        self.assertEqual(history[0]["reason"], "official feed delay")

    def test_missing_summary_names_partial_and_empty_forecasts(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_match(conn, "1", 2, "Liverpool", "Everton", "2026-08-15T20:30:00+03:00", None)
        upsert_prediction(conn, "Bruce Wayne", "1", 1, "2:1", "2026-08-15T14:00:00+03:00", "test")
        upsert_prediction(conn, "Igor", "1", 1, "1:0", "2026-08-15T14:00:00+03:00", "test")
        upsert_prediction(conn, "Igor", "1", 2, "1:1", "2026-08-15T14:00:00+03:00", "test")
        from brucebet.storage import ensure_participant

        ensure_participant(conn, "Anna", paid=1)

        item = missing_forecasts_summary(conn, "1")

        self.assertEqual(item["complete_count"], 1)
        self.assertEqual(
            [(row["participant"], row["missing_positions"]) for row in item["incomplete"]],
            [("Anna", (1, 2)), ("Bruce Wayne", (2,))],
        )

    def test_manual_forecast_override_preserves_submission_and_audits(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        match_id = upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T18:00:00+03:00", None)
        upsert_prediction(conn, "Igor", "1", 1, "1:0", "2026-08-15T14:00:00+03:00", "test")

        previous, current = set_manual_prediction_override(
            conn,
            "Igor",
            match_id,
            "2-1",
            actor_chat_id=42,
            reason="confirmed typo",
            changed_at="2026-08-15T17:00:00+03:00",
        )
        stored = conn.execute("SELECT score, submitted_at, source FROM predictions").fetchone()
        history = manual_prediction_history(conn, "Igor", match_id)

        self.assertEqual((previous, current), ("1:0", "2:1"))
        self.assertEqual((stored["score"], stored["submitted_at"], stored["source"]), ("2:1", "2026-08-15T14:00:00+03:00", "manual-override"))
        self.assertEqual(history[0]["reason"], "confirmed typo")

    def test_rehearsal_covers_operator_flow_without_live_data(self) -> None:
        item = run_rehearsal()

        self.assertTrue(all(bool(row["passed"]) for row in item["checks"]))
        self.assertEqual(item["missing"]["complete_count"], 5)
        self.assertEqual(len(item["standings"]), 5)


if __name__ == "__main__":
    unittest.main()
