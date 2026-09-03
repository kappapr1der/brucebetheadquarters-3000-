from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import unittest

from brucebet.analytics import round_review
from brucebet.storage import active_season_id, connect, reset_db, upsert_match
from brucebet.vk_board import VkPublicTopicCaptureResult, VkPublicTopicResult
from brucebet.vk_capture_state import capture_gate_for_round, record_vk_capture_state
from brucebet.vk_dry_run import parse_public_topic_capture_result
from brucebet.vk_parser import MSK
from brucebet.vk_prediction_import import VkPredictionImportError, import_vk_prediction_report, recover_vk_round
from brucebet.vk_prediction_notifications import pending_vk_prediction_notification_deliveries
from tests.test_vk_pagination_capture import GROUP, ROOT, TOPIC, forecast_text, page, template_text


FIXTURES = (
    ("Arsenal", "Chelsea", "2:1"),
    ("Liverpool", "Everton", "1:0"),
    ("Manchester City", "Tottenham", "3:1"),
    ("Newcastle", "Aston Villa", "1:0"),
    ("Brighton", "Fulham", "2:0"),
    ("Brentford", "Crystal Palace", "1:1"),
    ("Leeds", "Sunderland", "2:1"),
    ("West Ham", "Bournemouth", "0:0"),
    ("Nottingham Forest", "Wolves", "1:0"),
    ("Burnley", "Manchester United", "0:2"),
)
IGOR_SCORES = ("2:1", "1:0", "3:1", "2:1", "3:1", "0:3", "4:2", "0:3", "0:3", "2:1")


def forecast_with_scores(author: str, scores: tuple[str, ...], *, timestamp: str = "10 авг 2030 в 12:10") -> str:
    return "\n".join(
        (
            f"{author} {timestamp}",
            *(f"{home} - {away} {score}" for (home, away, _result), score in zip(FIXTURES, scores)),
        )
    )


def report_from_pages(*pages: VkPublicTopicResult, complete: bool = True, stop_reason: str = "pagination_exhausted"):
    capture = VkPublicTopicCaptureResult(
        group_id=GROUP,
        topic_id=TOPIC,
        url=ROOT,
        pages=tuple(pages),
        capture_complete=complete,
        stop_reason=stop_reason,
    )
    return replace(parse_public_topic_capture_result(capture, "predictions"), captured_at=datetime(2030, 8, 11, 13, tzinfo=MSK))


class VkRoundRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        reset_db(self.conn)
        for position, (home, away, result) in enumerate(FIXTURES, start=1):
            upsert_match(
                self.conn,
                "1",
                position,
                home,
                away,
                "2030-08-21T20:30:00+03:00",
                result,
                source="premierleague.com",
                source_fixture_id=f"fixture-{position}",
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _initial_report(self):
        comments = [
            page(template_text(), key="offset=20", comment_ids=("3538",)),
            page(
                "\n".join(
                    (
                        forecast_text("Mikhail Makarov"),
                        forecast_text("Mr Sam"),
                        forecast_text("Sergey Kirillov"),
                        forecast_text("Ефремычев Юрий"),
                    )
                ),
                key="root",
                comment_ids=("3539", "3542", "3541", "3544"),
            ),
        ]
        return report_from_pages(*comments)

    def _complete_report(self):
        initial = self._initial_report()
        return report_from_pages(
            page(template_text(), key="offset=20", comment_ids=("3538",)),
            page(
                "\n".join(
                    (
                        forecast_text("Mikhail Makarov"),
                        forecast_text("Mr Sam"),
                        forecast_text("Sergey Kirillov"),
                        forecast_text("Ефремычев Юрий"),
                        forecast_with_scores("Igor Grigoryev", IGOR_SCORES),
                    )
                ),
                key="root",
                comment_ids=("3539", "3542", "3541", "3544", "3555"),
            ),
        )

    def _import(self, report):
        return import_vk_prediction_report(
            self.conn,
            report,
            expected_group_id=GROUP,
            expected_topic_id=TOPIC,
            lock_minutes=90,
        )

    def _frozen_recommendation_snapshot(self) -> list[tuple[object, ...]]:
        season_id = active_season_id(self.conn)
        match = self.conn.execute("SELECT id, position, home, away FROM matches ORDER BY position LIMIT 1").fetchone()
        round_id = self.conn.execute("SELECT id FROM rounds WHERE name = '1'").fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO contest_recommendations(
                season_id, round_id, match_id, round_name, position, home, away,
                recommended_score, recommended_outcome, status, confidence, risk_level,
                model_suggested_score, model_probabilities_json, model_assessment_updated_at,
                field_prediction_count, field_expected_count, field_scores_json, field_outcomes_json,
                field_top_outcome, field_top_share, field_top_scores_json,
                market_present, market_captured_at, market_probabilities_json, market_top_outcome,
                market_top_share, strategy_mode, volatility, readiness_status, readiness_warnings_json,
                input_fingerprint, generated_at, frozen_final, freeze_reason, previous_recommendation_id
            )
            VALUES(?, ?, ?, '1', ?, ?, ?, '1:0', 'P1', 'final', NULL, NULL,
                   '1:0', '{}', NULL, 4, 4, '{}', '{}', NULL, NULL, '[]',
                   0, NULL, '{}', NULL, NULL, 'balanced', 0.1, 'ready', '[]',
                   'frozen-round-1', '2030-08-21T20:00:00+03:00', 1, 'pre_deadline_final', NULL)
            """,
            (season_id, round_id, int(match["id"]), int(match["position"]), match["home"], match["away"]),
        )
        self.conn.commit()
        return [tuple(row) for row in self.conn.execute("SELECT * FROM contest_recommendations ORDER BY id")]

    def test_recovery_reproduces_incident_and_recovers_igor_without_mutating_frozen_pick(self) -> None:
        first = self._import(self._initial_report())
        self.assertEqual(first.submissions_seen, 4)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM participants WHERE name = 'Igor Grigoryev'").fetchone()
        )
        frozen_before = self._frozen_recommendation_snapshot()

        recovered = recover_vk_round(
            self.conn,
            self._complete_report(),
            round_name="1",
            expected_group_id=GROUP,
            expected_topic_id=TOPIC,
            lock_minutes=90,
            notification_chat_ids=(99,),
        )

        self.assertEqual((recovered.import_report.revisions_created, recovered.import_report.accepted), (10, 10))
        self.assertEqual(recovered.import_report.recovered_participants, ("Igor Grigoryev",))
        igor = next(item for item in recovered.review["participants"] if item["participant"] == "Igor Grigoryev")
        self.assertEqual((igor["points"], igor["exact"], igor["diff"], igor["outcome"]), (14, 3, 2, 1))
        self.assertEqual(frozen_before, [tuple(row) for row in self.conn.execute("SELECT * FROM contest_recommendations ORDER BY id")])

        deliveries = pending_vk_prediction_notification_deliveries(self.conn, (99,))
        self.assertEqual([(item.kind, item.round_name) for item in deliveries], [("recovery_summary", "1")])
        repeated = recover_vk_round(
            self.conn,
            self._complete_report(),
            round_name="1",
            expected_group_id=GROUP,
            expected_topic_id=TOPIC,
            lock_minutes=90,
            notification_chat_ids=(99,),
        )
        self.assertEqual((repeated.import_report.revisions_created, repeated.import_report.duplicates), (0, 50))
        self.assertEqual(len(pending_vk_prediction_notification_deliveries(self.conn, (99,))), 1)

    def test_recovery_uses_original_timestamp_and_rejects_genuinely_late_submission(self) -> None:
        early = report_from_pages(
            page(template_text(), key="offset=20", comment_ids=("3538",)),
            page(forecast_with_scores("Igor Grigoryev", IGOR_SCORES), key="root", comment_ids=("3555",)),
        )
        accepted = recover_vk_round(
            self.conn,
            early,
            round_name="1",
            expected_group_id=GROUP,
            expected_topic_id=TOPIC,
        )
        self.assertEqual(accepted.import_report.accepted, 10)

        late_conn = connect(":memory:")
        try:
            reset_db(late_conn)
            for position, (home, away, result) in enumerate(FIXTURES, start=1):
                upsert_match(late_conn, "1", position, home, away, "2030-08-21T20:30:00+03:00", result, source="premierleague.com", source_fixture_id=f"late-{position}")
            late = report_from_pages(
                page(template_text(), key="offset=20", comment_ids=("3538",)),
                page(forecast_with_scores("Late Igor", IGOR_SCORES, timestamp="22 авг 2030 в 12:10"), key="root", comment_ids=("3556",)),
            )
            result = recover_vk_round(
                late_conn,
                late,
                round_name="1",
                expected_group_id=GROUP,
                expected_topic_id=TOPIC,
                notification_chat_ids=(77,),
            )
            self.assertEqual((result.import_report.accepted, result.import_report.rejected), (0, 10))
            self.assertEqual(
                [item.kind for item in pending_vk_prediction_notification_deliveries(late_conn, (77,))],
                ["recovery_summary"],
            )
        finally:
            late_conn.close()

    def test_incomplete_capture_is_rejected_and_blocks_the_round_gate_once(self) -> None:
        report = replace(self._initial_report(), capture_complete=False, capture_stop_reason="pagination_limit")
        with self.assertRaisesRegex(VkPredictionImportError, "incomplete"):
            self._import(report)
        first_state, first_warning = record_vk_capture_state(
            self.conn,
            report,
            score_line_count=40,
            notification_chat_ids=(99,),
        )
        _second_state, second_warning = record_vk_capture_state(
            self.conn,
            report,
            score_line_count=40,
            notification_chat_ids=(99,),
        )
        self.assertEqual((first_state.capture_complete, first_state.stop_reason, first_warning, second_warning), (False, "pagination_limit", True, False))
        self.assertEqual(capture_gate_for_round(self.conn, "1"), "vk_capture:pagination_limit")
        delivery = pending_vk_prediction_notification_deliveries(self.conn, (99,))[0]
        self.assertEqual((delivery.kind, delivery.payload["reason"]), ("field_incomplete", "pagination_limit"))
        complete_state, complete_warning = record_vk_capture_state(
            self.conn,
            self._initial_report(),
            score_line_count=40,
            notification_chat_ids=(99,),
        )
        self.assertEqual((complete_state.capture_complete, complete_warning, capture_gate_for_round(self.conn, "1")), (True, False, None))


if __name__ == "__main__":
    unittest.main()
