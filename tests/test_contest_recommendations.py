from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from brucebet.analytics import match_rows_for_round
from brucebet.vk_capture_state import record_vk_capture_failure
from brucebet.contest_recommendations import (
    due_final_contest_rounds,
    mark_contest_recommendation_delivery_failed,
    mark_contest_recommendation_delivery_sent,
    pending_contest_recommendation_deliveries,
    recompute_contest_recommendations,
    render_contest_prediction_template,
    render_contest_recommendation_update,
    render_contest_recommendations,
)
from tests.test_epl_headquarters import load_epl_sample


MSK = timezone(timedelta(hours=3))


class ContestRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = load_epl_sample()
        self.conn.execute("UPDATE matches SET kickoff_at = '2026-08-21T22:00:00+03:00'")
        self.conn.execute("UPDATE matches SET result = NULL")
        self.conn.commit()
        self.now = datetime(2026, 8, 21, 20, 0, tzinfo=MSK)

    def tearDown(self) -> None:
        self.conn.close()

    def ready_intelligence(self):
        matches = match_rows_for_round(self.conn, "1")
        return {
            "items": [
                {"match": match, "status": "ready", "follow_up": []}
                for match in matches
            ],
            "ready_count": len(matches),
            "attention_count": 0,
            "blocked_count": 0,
        }

    def recompute(self, **kwargs):
        return recompute_contest_recommendations(
            self.conn,
            round_name="1",
            now=self.now,
            **kwargs,
        )

    def make_field_complete(self) -> None:
        self.conn.execute(
            "UPDATE season_participants SET active = 0 WHERE participant_id = (SELECT id FROM participants WHERE name = 'Guest')"
        )
        self.conn.commit()

    def test_01_initial_pick_persists_separately_from_predictions(self) -> None:
        before_predictions = self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        before_model = [tuple(row) for row in self.conn.execute("SELECT match_id, suggested_score FROM match_assessments")]

        batch = self.recompute()

        self.assertEqual(len(batch.recommendations), 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contest_recommendations").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], before_predictions)
        self.assertEqual(
            [tuple(row) for row in self.conn.execute("SELECT match_id, suggested_score FROM match_assessments")],
            before_model,
        )

    def test_02_field_excludes_bruce_from_competitor_count(self) -> None:
        batch = self.recompute()

        first = batch.recommendations[0]
        self.assertEqual((first.field_prediction_count, first.field_expected_count), (3, 4))

    def test_03_incomplete_competitor_field_is_provisional(self) -> None:
        with patch("brucebet.contest_recommendations.intelligence_readiness", return_value=self.ready_intelligence()):
            batch = self.recompute()

        self.assertEqual((batch.field_complete_count, batch.field_expected_count), (3, 4))
        self.assertTrue(all(item.status == "provisional" for item in batch.recommendations))

    def test_04_complete_ready_field_is_ready(self) -> None:
        self.make_field_complete()
        with patch("brucebet.contest_recommendations.intelligence_readiness", return_value=self.ready_intelligence()):
            batch = self.recompute()

        self.assertEqual((batch.field_complete_count, batch.field_expected_count), (3, 3))
        self.assertTrue(all(item.status == "ready" for item in batch.recommendations))

    def test_04a_incomplete_vk_capture_keeps_field_provisional_and_blocks_final_freeze(self) -> None:
        self.make_field_complete()
        record_vk_capture_failure(
            self.conn,
            group_id=217130885,
            topic_id=67251746,
            topic_kind="predictions",
            reason="pagination_limit",
        )

        with patch("brucebet.contest_recommendations.intelligence_readiness", return_value=self.ready_intelligence()):
            batch = self.recompute()

        self.assertTrue(all(item.status == "provisional" for item in batch.recommendations))
        warnings = [row[0] for row in self.conn.execute("SELECT readiness_warnings_json FROM contest_recommendations")]
        self.assertTrue(all("vk_capture:pagination_limit" in item for item in warnings))
        with self.assertRaisesRegex(ValueError, "VK field is incomplete"):
            self.recompute(finalize=True)

    def test_05_missing_base_assessment_blocks_only_that_match(self) -> None:
        first_match = self.conn.execute("SELECT id FROM matches WHERE position = 1").fetchone()[0]
        self.conn.execute("DELETE FROM match_assessments WHERE match_id = ?", (first_match,))
        self.conn.commit()

        batch = self.recompute()

        blocked = next(item for item in batch.recommendations if item.match_id == first_match)
        self.assertEqual((blocked.status, blocked.recommended_score), ("blocked", ""))
        self.assertTrue(any(item.recommended_score for item in batch.recommendations if item.match_id != first_match))

    def test_06_opening_round_forces_balanced_strategy(self) -> None:
        batch = self.recompute()

        modes = {row[0] for row in self.conn.execute("SELECT strategy_mode FROM contest_recommendations")}
        self.assertEqual(modes, {"balanced"})
        self.assertEqual(batch.round_name, "1")

    def test_07_missing_market_is_recorded_without_blocking_model_pick(self) -> None:
        self.conn.execute("DELETE FROM match_odds")
        self.conn.commit()

        batch = self.recompute()

        self.assertEqual(batch.market_present_count, 0)
        self.assertTrue(all(item.recommended_score for item in batch.recommendations))

    def test_07a_incomplete_field_cannot_reverse_the_model_and_market_outcome(self) -> None:
        self.conn.execute(
            """
            UPDATE predictions
            SET score = '0:1'
            WHERE match_id = (SELECT id FROM matches WHERE position = 1)
              AND participant_id != (SELECT id FROM participants WHERE name = 'Bruce Wayne')
            """
        )
        self.conn.execute("INSERT INTO participants(name, paid) VALUES ('Late entrant', 0)")
        self.conn.execute(
            """
            INSERT INTO season_participants(season_id, participant_id, paid, active)
            VALUES(
                (SELECT id FROM seasons WHERE active = 1),
                (SELECT id FROM participants WHERE name = 'Late entrant'),
                0,
                1
            )
            """
        )
        self.conn.commit()

        incomplete = self.recompute()
        first_incomplete = incomplete.recommendations[0]
        self.assertEqual((first_incomplete.field_prediction_count, first_incomplete.field_expected_count), (3, 5))
        self.assertEqual(first_incomplete.recommended_outcome, "P1")
        warnings = json.loads(
            self.conn.execute(
                "SELECT readiness_warnings_json FROM contest_recommendations WHERE id = ?",
                (first_incomplete.id,),
            ).fetchone()["readiness_warnings_json"]
        )
        self.assertIn("field:below_outcome_threshold:3/5", warnings)

        self.conn.execute(
            "UPDATE season_participants SET active = 0 WHERE participant_id = (SELECT id FROM participants WHERE name = 'Late entrant')"
        )
        self.conn.commit()
        self.make_field_complete()
        complete = self.recompute()
        self.assertEqual(complete.recommendations[0].recommended_outcome, "P2")

    def test_08_unchanged_inputs_are_idempotent(self) -> None:
        first = self.recompute()
        second = self.recompute()

        self.assertTrue(first.recomputed)
        self.assertEqual(second.changed, ())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contest_recommendations").fetchone()[0], 4)

    def test_09_market_change_creates_a_new_auditable_history_row(self) -> None:
        self.recompute()
        self.conn.execute("UPDATE match_odds SET home_win = 1.2 WHERE match_id = (SELECT id FROM matches WHERE position = 1)")
        self.conn.commit()

        self.recompute()

        rows = self.conn.execute(
            "SELECT id, previous_recommendation_id FROM contest_recommendations WHERE position = 1 ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["previous_recommendation_id"], rows[0]["id"])

    def test_10_synthesis_never_inserts_bruce_prediction(self) -> None:
        self.conn.execute(
            "DELETE FROM predictions WHERE participant_id = (SELECT id FROM participants WHERE name = 'Bruce Wayne')"
        )
        self.conn.commit()

        self.recompute()

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE participant_id = (SELECT id FROM participants WHERE name = 'Bruce Wayne')"
            ).fetchone()[0],
            0,
        )

    def test_11_final_snapshot_is_frozen_and_notified(self) -> None:
        self.make_field_complete()
        with patch("brucebet.contest_recommendations.intelligence_readiness", return_value=self.ready_intelligence()):
            self.recompute()
            batch = self.recompute(finalize=True, enqueue_update=True, notification_chat_ids=(42,))

        self.assertTrue(batch.frozen_final)
        self.assertTrue(all(item.status == "final" for item in batch.recommendations))
        event = self.conn.execute("SELECT kind FROM contest_recommendation_notifications").fetchone()
        self.assertEqual(event["kind"], "final")
        self.assertIn("Финальный прогноз", render_contest_recommendations(batch, final=True))

    def test_12_final_snapshot_stays_provisional_when_field_is_incomplete(self) -> None:
        with patch("brucebet.contest_recommendations.intelligence_readiness", return_value=self.ready_intelligence()):
            batch = self.recompute(finalize=True)

        self.assertTrue(batch.frozen_final)
        self.assertTrue(all(item.status == "provisional" for item in batch.recommendations))
        self.assertIn("Статус: provisional.", render_contest_recommendations(batch, final=True))

    def test_13_final_scheduler_window_starts_at_configured_lead(self) -> None:
        before = due_final_contest_rounds(
            self.conn,
            now=datetime(2026, 8, 21, 20, 19, tzinfo=MSK),
            lock_minutes=90,
            lead_minutes=10,
        )
        at_window = due_final_contest_rounds(
            self.conn,
            now=datetime(2026, 8, 21, 20, 20, tzinfo=MSK),
            lock_minutes=90,
            lead_minutes=10,
        )

        self.assertEqual(before, ())
        self.assertEqual(at_window, ("1",))

    def test_14_final_scheduler_does_not_repeat_a_frozen_round(self) -> None:
        self.recompute(finalize=True)

        due = due_final_contest_rounds(
            self.conn,
            now=datetime(2026, 8, 21, 20, 20, tzinfo=MSK),
            lock_minutes=90,
            lead_minutes=10,
        )

        self.assertEqual(due, ())

    def test_15_post_deadline_recompute_returns_existing_ledger(self) -> None:
        before = self.recompute()
        self.conn.execute("DELETE FROM match_assessments")
        self.conn.commit()

        locked = recompute_contest_recommendations(
            self.conn,
            round_name="1",
            now=datetime(2026, 8, 21, 20, 31, tzinfo=MSK),
        )

        self.assertTrue(locked.deadline_locked)
        self.assertFalse(locked.recomputed)
        self.assertEqual([item.recommended_score for item in locked.recommendations], [item.recommended_score for item in before.recommendations])

    def test_16_final_title_never_claims_final_when_readiness_is_not_ready(self) -> None:
        batch = self.recompute(finalize=True)

        text = render_contest_recommendations(batch, final=True)
        self.assertNotIn("Статус: финальный.", text)

    def test_17_high_volatility_reduces_large_margin_to_canonical_score(self) -> None:
        self.conn.execute(
            "UPDATE match_assessments SET suggested_score = '3:0', home_edge = 0.8, draw_edge = 0.1, away_edge = 0.1, volatility = 0.9 WHERE match_id = (SELECT id FROM matches WHERE position = 1)"
        )
        self.conn.commit()

        batch = self.recompute()

        self.assertEqual(batch.recommendations[0].recommended_score, "1:0")

    def test_18_initial_outbox_is_durable_per_chat(self) -> None:
        self.recompute(enqueue_update=True, notification_chat_ids=(42, 77))

        deliveries = pending_contest_recommendation_deliveries(self.conn, (42, 77))
        self.assertEqual([item.chat_id for item in deliveries], [42, 77])
        mark_contest_recommendation_delivery_sent(self.conn, deliveries[0], sent_at="2026-08-21T20:00:01+03:00")
        mark_contest_recommendation_delivery_failed(
            self.conn,
            deliveries[1],
            "temporary telegram error",
            attempted_at="2026-08-21T20:00:02+03:00",
        )
        retry = pending_contest_recommendation_deliveries(self.conn, (42, 77))
        self.assertEqual([item.chat_id for item in retry], [77])

    def test_19_recommendation_delta_is_emitted_only_when_pick_changes(self) -> None:
        self.recompute(enqueue_update=True, notification_chat_ids=(42,))
        self.conn.execute("DELETE FROM match_assessments WHERE match_id = (SELECT id FROM matches WHERE position = 1)")
        self.conn.commit()

        batch = self.recompute(enqueue_update=True, notification_chat_ids=(42,))

        events = self.conn.execute("SELECT kind FROM contest_recommendation_notifications ORDER BY created_at, event_key").fetchall()
        self.assertEqual([row["kind"] for row in events], ["initial", "update"])
        self.assertIn("пересчитал", render_contest_recommendation_update(batch))

    def test_20_same_inputs_do_not_enqueue_a_second_event(self) -> None:
        self.recompute(enqueue_update=True, notification_chat_ids=(42,))
        self.recompute(enqueue_update=True, notification_chat_ids=(42,))

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM contest_recommendation_notifications").fetchone()[0], 1)

    def test_21_draft_and_copy_ready_template_use_russian_team_names(self) -> None:
        batch = self.recompute()

        draft = render_contest_recommendations(batch)
        template = render_contest_prediction_template(batch)

        self.assertIn("Арсенал — Челси", draft)
        self.assertNotIn("Arsenal", draft)
        self.assertIn("Брайтон — Ньюкасл", draft)
        self.assertIn("Тоттенхэм — Манчестер Юнайтед", draft)
        self.assertTrue(template.splitlines()[0].startswith("Арсенал - Челси "))
        self.assertNotIn("Arsenal", template)
        self.assertIn("Брайтон - Ньюкасл", template)
        self.assertIn("Тоттенхэм - Манчестер Юнайтед", template)
        self.assertIn("1 участник ещё не прислал", draft)

        five_missing = render_contest_recommendations(
            replace(batch, field_complete_count=8, field_expected_count=13)
        )
        self.assertIn("5 участников ещё не прислали", five_missing)


if __name__ == "__main__":
    unittest.main()
