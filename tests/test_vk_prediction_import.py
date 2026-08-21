from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import unittest

from brucebet.storage import connect, ensure_participant, reset_db, upsert_match
from brucebet.vk_board import VkPublicTopicResult
from brucebet.vk_dry_run import parse_public_topic_result
from brucebet.vk_parser import MSK
from brucebet.vk_prediction_import import VkPredictionImportError, import_vk_prediction_report
from brucebet.vk_prediction_notifications import (
    mark_vk_prediction_notification_delivery_sent,
    pending_vk_prediction_notification_deliveries,
    render_vk_prediction_notification,
)


MATCHES = (
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


def prediction_text(
    first_score: str = "2:1",
    participant: str = "Сергей",
    scores: tuple[str, ...] | None = None,
) -> str:
    scores = scores or (first_score, *(score for _home, _away, score in MATCHES[1:]))
    template_lines = "\n".join(f"{home} - {away}" for home, away, _score in MATCHES)
    forecast_lines = "\n".join(
        f"{home} - {away} {score}" for (home, away, _default), score in zip(MATCHES, scores)
    )
    return f"""
Forecasters Club
Прогнозы на АПЛ 2030/2031
Forecasters Club 9 авг 2030 в 10:00
Шаблон на АПЛ, 1-й тур. Дедлайн 21.08.2030, 20:30
{template_lines}
Mr Sam
10 авг 2030 в 12:10
{participant}
{forecast_lines}
"""


def make_report(
    first_score: str = "2:1",
    *,
    participant: str = "Сергей",
    captured_at: datetime | None = None,
    scores: tuple[str, ...] | None = None,
):
    text = prediction_text(first_score, participant, scores)
    result = VkPublicTopicResult(
        group_id=217130885,
        topic_id=67251746,
        url="https://vk.ru/topic-217130885_67251746",
        html_chars=len(text),
        visible_chars=len(text),
        score_line_count=10,
        text=text,
    )
    parsed = parse_public_topic_result(result, "predictions")
    return replace(parsed, captured_at=captured_at or datetime(2030, 8, 10, 13, 0, tzinfo=MSK))


def prepare_matches(conn) -> None:
    for template_position, (home, away, _score) in enumerate(MATCHES, start=1):
        upsert_match(
            conn,
            "1",
            11 - template_position,
            home,
            away,
            "2030-08-21T20:30:00+03:00",
            None,
            source="premierleague.com",
            source_fixture_id=f"fixture-{template_position}",
        )


class VkPredictionImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        reset_db(self.conn)
        prepare_matches(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def import_report(self, report, *, notification_chat_ids: tuple[int, ...] = ()):
        return import_vk_prediction_report(
            self.conn,
            report,
            expected_group_id=217130885,
            expected_topic_id=67251746,
            lock_minutes=90,
            notification_chat_ids=notification_chat_ids,
        )

    def notification_rows(self):
        return self.conn.execute(
            "SELECT event_key, kind, payload_json FROM vk_prediction_notifications ORDER BY created_at, event_key"
        ).fetchall()

    def test_pair_mapping_survives_position_reorder_and_repeat_is_noop(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()

        first = self.import_report(report)
        repeated = self.import_report(report)

        self.assertEqual((first.revisions_created, first.accepted, first.duplicates), (10, 10, 0))
        self.assertEqual((repeated.revisions_created, repeated.accepted, repeated.duplicates), (0, 0, 10))
        self.assertEqual(first.accepted_rounds, ("1",))
        self.assertEqual(repeated.accepted_rounds, ())
        arsenal = self.conn.execute(
            """
            SELECT m.position, pr.score
            FROM predictions pr JOIN matches m ON m.id = pr.match_id
            WHERE m.home = 'Arsenal' AND m.away = 'Chelsea'
            """
        ).fetchone()
        self.assertEqual((arsenal["position"], arsenal["score"]), (10, "2:1"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 10)

    def test_comment_edit_uses_observation_time_and_cannot_overwrite_after_deadline(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        self.import_report(make_report("2:1"))

        early_edit = self.import_report(
            make_report("1:1", captured_at=datetime(2030, 8, 11, 12, 0, tzinfo=MSK))
        )
        late_edit = self.import_report(
            make_report("0:0", captured_at=datetime(2030, 8, 22, 12, 0, tzinfo=MSK))
        )

        self.assertEqual((early_edit.revisions_created, early_edit.accepted), (1, 1))
        self.assertEqual((late_edit.revisions_created, late_edit.rejected), (1, 1))
        score = self.conn.execute(
            """
            SELECT pr.score
            FROM predictions pr JOIN matches m ON m.id = pr.match_id
            WHERE m.home = 'Arsenal' AND m.away = 'Chelsea'
            """
        ).fetchone()[0]
        self.assertEqual(score, "1:1")
        revision = self.conn.execute(
            """
            SELECT source_submitted_at, eligibility_at, observed_at,
                   eligibility_decision, reason, projected
            FROM prediction_revisions rev
            JOIN matches m ON m.id = rev.match_id
            WHERE m.home = 'Arsenal' AND m.away = 'Chelsea'
            ORDER BY rev.id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(revision["source_submitted_at"], "2030-08-10T12:10:00+03:00")
        self.assertEqual(revision["eligibility_at"], "2030-08-22T12:00:00+03:00")
        self.assertEqual(
            (revision["eligibility_decision"], revision["reason"], revision["projected"]),
            ("rejected", "late_edit", 0),
        )

    def test_chromium_ordinal_reorder_does_not_create_revisions(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()
        self.import_report(report)
        submission = report.forecast_submissions[0]
        reordered = replace(submission, source_key=f"{submission.source_key}:99")
        reordered_report = replace(report, forecast_submissions=(reordered,))

        result = self.import_report(reordered_report)

        self.assertEqual((result.revisions_created, result.duplicates), (0, 10))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 10)

    def test_valid_forecast_enrolls_unknown_participant_as_unpaid_and_is_idempotent(self) -> None:
        report = make_report(participant="Незнакомец")

        first = self.import_report(report)
        repeated = self.import_report(report)

        self.assertEqual((first.revisions_created, first.accepted, first.quarantined), (10, 10, 0))
        self.assertEqual((repeated.revisions_created, repeated.duplicates, repeated.quarantined), (0, 10, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 10)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 10)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vk_prediction_quarantine").fetchone()[0], 0)
        participant = self.conn.execute(
            """
            SELECT p.name, p.paid, sp.paid AS season_paid, sp.active
            FROM participants p
            JOIN season_participants sp ON sp.participant_id = p.id
            WHERE p.name = 'Незнакомец'
            """
        ).fetchone()
        self.assertEqual(tuple(participant), ("Незнакомец", 0, 0, 1))

    def test_first_forecast_enqueues_one_grouped_notification_and_replay_is_silent(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()

        first = self.import_report(report, notification_chat_ids=(42, 77))
        repeated = self.import_report(report, notification_chat_ids=(42, 77))

        self.assertEqual((first.revisions_created, first.notification_events_created), (10, 1))
        self.assertEqual((repeated.revisions_created, repeated.notification_events_created), (0, 0))
        rows = self.notification_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "new")
        self.assertEqual(json.loads(rows[0]["payload_json"])["accepted"], 10)
        deliveries = pending_vk_prediction_notification_deliveries(self.conn, (42, 77))
        self.assertEqual([(item.chat_id, item.kind) for item in deliveries], [(42, "new"), (77, "new")])
        text = render_vk_prediction_notification(deliveries[0])
        self.assertIn("🎯 Новый прогноз — Тур 1", text)
        self.assertIn("Принято: 10/10", text)

    def test_import_delivery_replay_rehearsal_never_duplicates_a_sent_event(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()

        imported = self.import_report(report, notification_chat_ids=(42,))
        deliveries = pending_vk_prediction_notification_deliveries(self.conn, (42,))
        self.assertEqual((imported.revisions_created, len(deliveries)), (10, 1))
        mark_vk_prediction_notification_delivery_sent(
            self.conn,
            deliveries[0],
            sent_at="2030-08-10T13:01:00+03:00",
        )

        replayed = self.import_report(report, notification_chat_ids=(42,))

        self.assertEqual((replayed.revisions_created, replayed.notification_events_created), (0, 0))
        self.assertEqual(pending_vk_prediction_notification_deliveries(self.conn, (42,)), [])
        sent = self.conn.execute(
            "SELECT status, sent_at FROM vk_prediction_notification_deliveries"
        ).fetchone()
        self.assertEqual((sent["status"], sent["sent_at"]), ("sent", "2030-08-10T13:01:00+03:00"))

    def test_one_and_many_match_edits_each_enqueue_one_grouped_event(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        self.import_report(make_report(), notification_chat_ids=(42,))

        one_match = self.import_report(
            make_report("1:1", captured_at=datetime(2030, 8, 11, 12, 0, tzinfo=MSK)),
            notification_chat_ids=(42,),
        )
        self.assertEqual((one_match.revisions_created, one_match.notification_events_created), (1, 1))
        one_event = self.notification_rows()[-1]
        self.assertEqual(one_event["kind"], "edit")
        one_payload = json.loads(one_event["payload_json"])
        self.assertEqual(len(one_payload["changes"]), 1)
        self.assertIn("Arsenal — Chelsea: 2:1 → 1:1", render_vk_prediction_notification(
            pending_vk_prediction_notification_deliveries(self.conn, (42,))[-1]
        ))

        scores = ("3:0", "2:2", *(score for _home, _away, score in MATCHES[2:]))
        many_match = self.import_report(
            make_report(captured_at=datetime(2030, 8, 12, 12, 0, tzinfo=MSK), scores=scores),
            notification_chat_ids=(42,),
        )
        self.assertEqual((many_match.revisions_created, many_match.notification_events_created), (2, 1))
        many_event = self.notification_rows()[-1]
        self.assertEqual(many_event["kind"], "edit")
        self.assertEqual(len(json.loads(many_event["payload_json"])["changes"]), 2)

    def test_late_edit_alert_preserves_current_projection(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        self.import_report(make_report(), notification_chat_ids=(42,))
        self.import_report(
            make_report("1:1", captured_at=datetime(2030, 8, 11, 12, 0, tzinfo=MSK)),
            notification_chat_ids=(42,),
        )

        late = self.import_report(
            make_report("0:0", captured_at=datetime(2030, 8, 22, 12, 0, tzinfo=MSK)),
            notification_chat_ids=(42,),
        )

        self.assertEqual((late.revisions_created, late.rejected, late.notification_events_created), (1, 1, 1))
        event = self.notification_rows()[-1]
        self.assertEqual(event["kind"], "late_edit")
        late_delivery = pending_vk_prediction_notification_deliveries(self.conn, (42,))[-1]
        rendered = render_vk_prediction_notification(late_delivery)
        self.assertIn("⛔ Поздняя правка отклонена", rendered)
        self.assertIn("Arsenal — Chelsea: 1:1 → 0:0 (текущий: 1:1)", rendered)
        self.assertIn("Текущий прогноз сохранён без изменений.", rendered)
        score = self.conn.execute(
            """
            SELECT prediction.score
            FROM predictions prediction
            JOIN matches match ON match.id = prediction.match_id
            WHERE match.home = 'Arsenal' AND match.away = 'Chelsea'
            """
        ).fetchone()[0]
        self.assertEqual(score, "1:1")

    def test_auto_enrollment_uses_a_normal_forecast_notification(self) -> None:
        report = make_report(participant="Незнакомец")

        first = self.import_report(report, notification_chat_ids=(42,))
        repeated = self.import_report(report, notification_chat_ids=(42,))

        self.assertEqual((first.accepted, first.quarantined, first.notification_events_created), (10, 0, 1))
        self.assertEqual((repeated.duplicates, repeated.notification_events_created), (10, 0))
        event = self.notification_rows()[0]
        self.assertEqual(event["kind"], "new")
        rendered = render_vk_prediction_notification(pending_vk_prediction_notification_deliveries(self.conn, (42,))[0])
        self.assertIn("🎯 Новый прогноз — Тур 1", rendered)
        self.assertIn("Незнакомец", rendered)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM participants WHERE name = 'Незнакомец'").fetchone())

    def test_fixture_mismatch_quarantines_the_whole_submission(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        self.conn.execute("UPDATE matches SET home = 'Wrong Team' WHERE home = 'Arsenal'")

        result = self.import_report(make_report())

        self.assertEqual((result.revisions_created, result.quarantined, len(result.issues)), (0, 1, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)

    def test_partial_forecast_block_is_quarantined_without_partial_writes(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()
        submission = report.forecast_submissions[0]
        partial = replace(submission, forecasts=submission.forecasts[:-1], status="partial")

        result = self.import_report(replace(report, forecast_submissions=(partial,)))

        self.assertEqual((result.revisions_created, result.quarantined, len(result.issues)), (0, 1, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 0)
        reason = self.conn.execute("SELECT reason FROM vk_prediction_quarantine").fetchone()[0]
        self.assertIn("incomplete", reason)

    def test_non_epl_or_wrong_topic_is_rejected_before_any_write(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        non_epl = replace(make_report(), league_hint="non_epl")

        with self.assertRaisesRegex(VkPredictionImportError, "EPL gate"):
            self.import_report(non_epl)
        with self.assertRaisesRegex(VkPredictionImportError, "configured group/topic"):
            import_vk_prediction_report(
                self.conn,
                make_report(),
                expected_group_id=217130885,
                expected_topic_id=999,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vk_prediction_quarantine").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
