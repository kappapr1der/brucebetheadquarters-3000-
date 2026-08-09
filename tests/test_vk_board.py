from __future__ import annotations

from types import SimpleNamespace
import unittest

from brucebet.vk_board import (
    build_topic_url,
    classify_topic,
    chromium_command,
    extract_topic_links,
    extract_visible_text,
    parse_topic_url,
    probe_public_group_topics,
    probe_public_topic,
)


class VkBoardTests(unittest.TestCase):
    def test_parse_vk_topic_urls(self) -> None:
        self.assertEqual(parse_topic_url("https://vk.ru/topic-217130885_66960850"), (217130885, 66960850))
        self.assertEqual(
            parse_topic_url("https://vk.com/topic-217130885_51728798?offset=20"),
            (217130885, 51728798),
        )
        self.assertEqual(build_topic_url(217130885, 66960850), "https://vk.ru/topic-217130885_66960850")

    def test_visible_text_ignores_script_style_and_noscript(self) -> None:
        html = """
        <html><body>
          <script>secret script</script>
          <style>.hidden { display:none }</style>
          <noscript>enable javascript</noscript>
          <h1>Forecasters Club</h1>
          <div>Arsenal - Chelsea 2:1</div>
        </body></html>
        """
        text = extract_visible_text(html)
        self.assertIn("Forecasters Club", text)
        self.assertIn("Arsenal - Chelsea 2:1", text)
        self.assertNotIn("secret script", text)
        self.assertNotIn("display:none", text)
        self.assertNotIn("enable javascript", text)

    def test_chromium_command_is_read_only_headless_dump(self) -> None:
        command = chromium_command(
            "https://vk.ru/topic-217130885_66960850",
            chromium_bin="/usr/bin/chromium",
            virtual_time_ms=9000,
        )
        self.assertEqual(command[0], "/usr/bin/chromium")
        self.assertIn("--headless", command)
        self.assertIn("--dump-dom", command)
        self.assertIn("--virtual-time-budget=9000", command)
        self.assertEqual(command[-1], "https://vk.ru/topic-217130885_66960850")

    def test_probe_public_topic_extracts_prediction_lines(self) -> None:
        html = """
        <!doctype html><html><body>
        <div>Forecasters Club</div>
        <div>Прогнозы на АПЛ 2026/2027</div>
        <div>Bruce Wayne</div>
        <div>Arsenal - Chelsea 2:1</div>
        <div>Liverpool - Everton 1-0</div>
        </body></html>
        """

        def runner(command, **kwargs):
            self.assertIn("--dump-dom", command)
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        result = probe_public_topic(217130885, 12345678, runner=runner)
        self.assertEqual(result.group_id, 217130885)
        self.assertEqual(result.topic_id, 12345678)
        self.assertEqual(result.score_line_count, 2)
        self.assertIn("Bruce Wayne", result.text)

    def test_group_discovery_extracts_and_classifies_public_topic_links(self) -> None:
        html = """
        <html><body>
          <a href="/topic-217130885_777">Прогнозы на АПЛ 2026/27</a>
          <a href="https://vk.ru/topic-217130885_778">Регистрация участников АПЛ</a>
          <a href="/topic-217130885_779">Прогнозы РПЛ</a>
          <a href="/topic-999_111">Чужая тема</a>
        </body></html>
        """
        topics = extract_topic_links(html, 217130885)

        self.assertEqual([topic.topic_id for topic in topics], [779, 778, 777])
        self.assertEqual((topics[1].league_hint, topics[1].topic_kind), ("epl", "registration"))
        self.assertTrue(topics[2].is_epl_candidate)
        self.assertFalse(topics[0].is_epl_candidate)
        self.assertEqual(classify_topic("Что-то ещё"), ("other", "unknown"))

    def test_group_discovery_uses_public_topics_page_before_group_home(self) -> None:
        html = '<a href="/topic-217130885_777">Прогнозы на АПЛ</a>'
        seen_urls: list[str] = []

        def runner(command, **kwargs):
            seen_urls.append(command[-1])
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        result = probe_public_group_topics(217130885, runner=runner)
        self.assertEqual(len(result.topics), 1)
        self.assertEqual(seen_urls, ["https://vk.ru/club217130885?act=topics"])


if __name__ == "__main__":
    unittest.main()
