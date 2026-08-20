from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import unittest

from brucebet.storage import connect, ensure_participant, reset_db, upsert_match
from brucebet.vk_board import VkPublicTopicResult
from brucebet.vk_dry_run import parse_public_topic_result
from brucebet.vk_parser import MSK
from brucebet.vk_prediction_import import VkPredictionImportError, import_vk_prediction_report


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


def prediction_text(first_score: str = "2:1", participant: str = "Сергей") -> str:
    scores = [first_score, *(score for _home, _away, score in MATCHES[1:])]
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
):
    text = prediction_text(first_score, participant)
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

    def import_report(self, report):
        return import_vk_prediction_report(
            self.conn,
            report,
            expected_group_id=217130885,
            expected_topic_id=67251746,
            lock_minutes=90,
        )

    def test_pair_mapping_survives_position_reorder_and_repeat_is_noop(self) -> None:
        ensure_participant(self.conn, "Сергей", paid=1)
        report = make_report()

        first = self.import_report(report)
        repeated = self.import_report(report)

        self.assertEqual((first.revisions_created, first.accepted, first.duplicates), (10, 10, 0))
        self.assertEqual((repeated.revisions_created, repeated.accepted, repeated.duplicates), (0, 0, 10))
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

    def test_unknown_participant_is_idempotently_quarantined_without_enrollment(self) -> None:
        report = make_report(participant="Незнакомец")

        first = self.import_report(report)
        repeated = self.import_report(report)

        self.assertEqual((first.quarantined, len(first.issues)), (1, 1))
        self.assertEqual((repeated.quarantined, len(repeated.issues)), (0, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prediction_revisions").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM vk_prediction_quarantine").fetchone()[0], 1)
        self.assertIsNone(
            self.conn.execute("SELECT id FROM participants WHERE name = 'Незнакомец'").fetchone()
        )

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
