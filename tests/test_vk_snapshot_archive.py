from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from brucebet.vk_board import VkPublicTopicResult
from brucebet.vk_dry_run import VkPublicTopicCapture, parse_public_topic_result
from brucebet.vk_snapshot_archive import archive_public_topic_capture


def make_capture(text: str) -> VkPublicTopicCapture:
    result = VkPublicTopicResult(
        group_id=217130885,
        topic_id=67251746,
        url="https://vk.ru/topic-217130885_67251746",
        html_chars=len(text) + 20,
        visible_chars=len(text),
        score_line_count=1,
        text=text,
    )
    return VkPublicTopicCapture(
        report=parse_public_topic_result(result, "predictions"),
        visible_text=text,
        html_chars=result.html_chars,
        visible_chars=result.visible_chars,
        score_line_count=result.score_line_count,
    )


class VkSnapshotArchiveTests(unittest.TestCase):
    def test_archives_changed_field_once_and_keeps_latest_auditable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            out_dir = Path(temp_dir)
            initial = make_capture("Прогнозы на АПЛ\nАрсенал - Челси 2:1\n")
            first = archive_public_topic_capture(out_dir, initial)
            repeated = archive_public_topic_capture(out_dir, initial)
            changed = archive_public_topic_capture(out_dir, make_capture("Прогнозы на АПЛ\nАрсенал - Челси 1:1\n"))

            self.assertTrue(first.created)
            self.assertTrue(first.path.exists())
            self.assertFalse(repeated.created)
            self.assertEqual(repeated.path, first.latest_path)
            self.assertTrue(changed.created)
            self.assertNotEqual(changed.path, first.path)
            self.assertTrue(changed.latest_path.exists())
            self.assertIn("Арсенал - Челси 1:1", changed.latest_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
