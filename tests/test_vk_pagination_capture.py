from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
import unittest

from brucebet.vk_board import (
    VkPublicTopicCaptureResult,
    VkPublicTopicResult,
    capture_public_topic,
    canonical_topic_pagination_url,
    extract_topic_comment_ids,
    extract_topic_pagination_links,
)
from brucebet.vk_dry_run import parse_public_topic_capture_result
from brucebet.vk_parser import MSK


GROUP = 217130885
TOPIC = 67251746
ROOT = f"https://vk.ru/topic-{GROUP}_{TOPIC}"


def page(text: str, *, key: str, comment_ids: tuple[str, ...] = ()) -> VkPublicTopicResult:
    return VkPublicTopicResult(
        group_id=GROUP,
        topic_id=TOPIC,
        url=ROOT if key == "root" else f"{ROOT}?{key}",
        html_chars=len(text),
        visible_chars=len(text),
        score_line_count=sum(1 for line in text.splitlines() if ":" in line and " - " in line),
        text=text,
        page_key=key,
        comment_ids=comment_ids,
    )


def template_text() -> str:
    fixtures = "\n".join(
        (
            "Arsenal - Chelsea",
            "Liverpool - Everton",
            "Manchester City - Tottenham",
            "Newcastle - Aston Villa",
            "Brighton - Fulham",
            "Brentford - Crystal Palace",
            "Leeds - Sunderland",
            "West Ham - Bournemouth",
            "Nottingham Forest - Wolves",
            "Burnley - Manchester United",
        )
    )
    return "\n".join(
        (
            "Forecasters Club",
            "Прогнозы на АПЛ 2030/2031",
            "Forecasters Club 9 авг 2030 в 10:00",
            "Шаблон на АПЛ, 1-й тур. Дедлайн 21.08.2030, 20:30",
            fixtures,
        )
    )


def forecast_text(author: str, *, first_score: str = "2:1") -> str:
    scores = (first_score, "1:0", "3:1", "1:0", "2:0", "1:1", "2:1", "0:0", "1:0", "0:2")
    fixtures = (
        "Arsenal - Chelsea",
        "Liverpool - Everton",
        "Manchester City - Tottenham",
        "Newcastle - Aston Villa",
        "Brighton - Fulham",
        "Brentford - Crystal Palace",
        "Leeds - Sunderland",
        "West Ham - Bournemouth",
        "Nottingham Forest - Wolves",
        "Burnley - Manchester United",
    )
    return "\n".join((f"{author} 10 авг 2030 в 12:10", *(f"{label} {score}" for label, score in zip(fixtures, scores))))


class VkPaginationCaptureTests(unittest.TestCase):
    def test_safe_url_requires_rendered_same_topic_pagination_parameter(self) -> None:
        self.assertEqual(
            canonical_topic_pagination_url(
                "?act=comments&offset=20",
                base_url=ROOT,
                group_id=GROUP,
                topic_id=TOPIC,
            ),
            f"{ROOT}?act=comments&offset=20",
        )
        self.assertIsNone(
            canonical_topic_pagination_url(
                "?post=3557",
                base_url=ROOT,
                group_id=GROUP,
                topic_id=TOPIC,
            )
        )

    def test_extracts_only_safe_pagination_and_public_comment_ids(self) -> None:
        html = f"""
        <a href="?offset=20" rel="next">Следующая</a>
        <a href="?post=3557">permalink</a>
        <a href="/topic-{GROUP}_{TOPIC}?offset=40">3</a>
        <a href="/topic-{GROUP}_{TOPIC}?post=3558">permalink 2</a>
        """
        links = extract_topic_pagination_links(html, base_url=ROOT, group_id=GROUP, topic_id=TOPIC)
        self.assertEqual([item.url for item in links], [f"{ROOT}?offset=20", f"{ROOT}?offset=40"])
        self.assertTrue(links[0].is_next)
        self.assertEqual(extract_topic_comment_ids(html, base_url=ROOT, group_id=GROUP, topic_id=TOPIC), ("3557", "3558"))

    def test_single_page_capture_is_complete(self) -> None:
        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="<div>one page</div>", stderr="")

        capture = capture_public_topic(GROUP, TOPIC, runner=runner)
        self.assertTrue(capture.capture_complete)
        self.assertEqual((len(capture.pages), capture.stop_reason), (1, "pagination_exhausted"))

    def test_multiple_comments_without_navigation_are_not_declared_complete(self) -> None:
        html = f"""
        <a href="{ROOT}?post=3538">first</a>
        <a href="{ROOT}?post=3539">second</a>
        """

        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        capture = capture_public_topic(GROUP, TOPIC, runner=runner)
        self.assertEqual((capture.capture_complete, capture.stop_reason), (False, "pagination_unproven"))

    def test_two_pages_are_followed_from_real_dom_link(self) -> None:
        html = {
            ROOT: '<a href="?offset=20" rel="next">Next</a><div>first</div>',
            f"{ROOT}?offset=20": "<div>second</div>",
        }
        seen: list[str] = []

        def runner(command, **kwargs):
            seen.append(command[-1])
            return SimpleNamespace(returncode=0, stdout=html[command[-1]], stderr="")

        capture = capture_public_topic(GROUP, TOPIC, runner=runner)
        self.assertTrue(capture.capture_complete)
        self.assertEqual(seen, [ROOT, f"{ROOT}?offset=20"])
        self.assertEqual(tuple(item.page_key for item in capture.pages), ("root", "offset=20"))

    def test_three_pages_are_followed_until_navigation_is_exhausted(self) -> None:
        html = {
            ROOT: '<a href="?offset=20" rel="next">Next</a><div>first</div>',
            f"{ROOT}?offset=20": '<a href="?offset=40" rel="next">Next</a><div>second</div>',
            f"{ROOT}?offset=40": "<div>third</div>",
        }

        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=html[command[-1]], stderr="")

        capture = capture_public_topic(GROUP, TOPIC, runner=runner)
        self.assertTrue(capture.capture_complete)
        self.assertEqual(tuple(item.page_key for item in capture.pages), ("root", "offset=20", "offset=40"))

    def test_template_on_one_page_and_submission_on_another_are_reconstructed(self) -> None:
        capture = VkPublicTopicCaptureResult(
            group_id=GROUP,
            topic_id=TOPIC,
            url=ROOT,
            pages=(page(template_text(), key="offset=20", comment_ids=("3538",)), page(forecast_text("Игорь Григорьев"), key="root", comment_ids=("3555",))),
            capture_complete=True,
            stop_reason="pagination_exhausted",
        )
        report = replace(parse_public_topic_capture_result(capture, "predictions"), captured_at=datetime(2030, 8, 10, 13, tzinfo=MSK))
        self.assertEqual(len(report.templates), 1)
        self.assertEqual(len(report.forecast_submissions), 1)
        submission = report.forecast_submissions[0]
        self.assertEqual((submission.participant, submission.source_key, len(submission.forecasts)), ("Игорь Григорьев", f"vk-public:{GROUP}:{TOPIC}:post:3555", 10))

    def test_overlap_deduplicates_comment_by_immutable_post_id(self) -> None:
        forecast = forecast_text("Игорь Григорьев")
        capture = VkPublicTopicCaptureResult(
            group_id=GROUP,
            topic_id=TOPIC,
            url=ROOT,
            pages=(page(template_text() + "\n" + forecast, key="root", comment_ids=("3538", "3555")), page(forecast, key="offset=20", comment_ids=("3555",))),
            capture_complete=True,
            stop_reason="pagination_exhausted",
        )
        report = parse_public_topic_capture_result(capture, "predictions")
        self.assertEqual(len(report.comments), 2)
        self.assertEqual(len(report.forecast_submissions), 1)

    def test_same_author_timestamp_collision_is_retained_as_ambiguous_not_merged(self) -> None:
        capture = VkPublicTopicCaptureResult(
            group_id=GROUP,
            topic_id=TOPIC,
            url=ROOT,
            pages=(
                page(template_text() + "\n" + forecast_text("Игорь Григорьев", first_score="2:1"), key="root"),
                page(forecast_text("Игорь Григорьев", first_score="1:1"), key="offset=20"),
            ),
            capture_complete=True,
            stop_reason="pagination_exhausted",
        )
        report = parse_public_topic_capture_result(capture, "predictions")
        self.assertEqual(len(report.forecast_submissions), 2)
        self.assertTrue(all(item.source_key.startswith("vk-ambiguous:") for item in report.forecast_submissions))

    def test_identical_scores_from_different_participants_are_preserved(self) -> None:
        capture = VkPublicTopicCaptureResult(
            group_id=GROUP,
            topic_id=TOPIC,
            url=ROOT,
            pages=(
                page(
                    "\n".join((template_text(), forecast_text("Игорь Григорьев"), forecast_text("Mr Sam"))),
                    key="root",
                    comment_ids=("3538", "3555", "3556"),
                ),
            ),
            capture_complete=True,
            stop_reason="pagination_exhausted",
        )
        report = parse_public_topic_capture_result(capture, "predictions")
        self.assertEqual([item.participant for item in report.forecast_submissions], ["Игорь Григорьев", "Mr Sam"])
        normalized = [
            [(item.match_label, item.normalized_score) for item in submission.forecasts]
            for submission in report.forecast_submissions
        ]
        self.assertEqual(normalized[0], normalized[1])

    def test_cycle_repeated_page_and_limit_are_incomplete(self) -> None:
        pages = {
            ROOT: '<a href="?offset=20" rel="next">Next</a>root',
            f"{ROOT}?offset=20": '<a href="?offset=0" rel="next">Next</a>second',
            f"{ROOT}?offset=0": '<a href="?offset=20" rel="next">Next</a>third',
        }

        def cycle_runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=pages[command[-1]], stderr="")

        cycle = capture_public_topic(GROUP, TOPIC, runner=cycle_runner)
        self.assertEqual((cycle.capture_complete, cycle.stop_reason), (False, "pagination_cycle"))

        def repeated_runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout='<a href="?offset=20">2</a>same', stderr="")

        repeated = capture_public_topic(GROUP, TOPIC, runner=repeated_runner)
        self.assertEqual((repeated.capture_complete, repeated.stop_reason), (False, "repeated_page_fingerprint"))

        def limit_runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout='<a href="?offset=20">2</a>root', stderr="")

        limited = capture_public_topic(GROUP, TOPIC, runner=limit_runner, max_pages=1)
        self.assertEqual((limited.capture_complete, limited.stop_reason), (False, "pagination_limit"))

    def test_later_page_challenge_is_a_partial_incomplete_capture(self) -> None:
        def runner(command, **kwargs):
            if command[-1] == ROOT:
                return SimpleNamespace(returncode=0, stdout='<a href="?offset=20">2</a>root', stderr="")
            return SimpleNamespace(returncode=0, stdout="Проверяем, что вы не робот", stderr="")

        capture = capture_public_topic(GROUP, TOPIC, runner=runner)
        self.assertEqual((capture.capture_complete, capture.stop_reason, len(capture.pages)), (False, "vk_challenge", 1))

    def test_later_page_error_is_partial_and_empty_page_is_incomplete(self) -> None:
        def error_runner(command, **kwargs):
            if command[-1] == ROOT:
                return SimpleNamespace(returncode=0, stdout='<a href="?offset=20">2</a>root', stderr="")
            return SimpleNamespace(returncode=7, stdout="", stderr="renderer failed")

        failed = capture_public_topic(GROUP, TOPIC, runner=error_runner)
        self.assertEqual((failed.capture_complete, failed.stop_reason, len(failed.pages)), (False, "page_error", 1))

        def empty_runner(command, **kwargs):
            if command[-1] == ROOT:
                return SimpleNamespace(returncode=0, stdout='<a href="?offset=20">2</a>root', stderr="")
            return SimpleNamespace(returncode=0, stdout="<div></div>", stderr="")

        empty = capture_public_topic(GROUP, TOPIC, runner=empty_runner)
        self.assertEqual((empty.capture_complete, empty.stop_reason, len(empty.pages)), (False, "empty_page", 2))


if __name__ == "__main__":
    unittest.main()
