from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from brucebet.vk_board import (
    VkAccessChallengeError,
    build_topic_url,
    classify_topic,
    chromium_command,
    extract_topic_links,
    extract_visible_text,
    parse_topic_url,
    probe_public_group_topics,
    probe_public_topic,
    run_chromium_command,
)


class VkBoardTests(unittest.TestCase):
    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def assert_pid_exits(self, pid: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._pid_exists(pid):
            time.sleep(0.02)
        self.assertFalse(self._pid_exists(pid), f"process {pid} was not cleaned up")

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
        self.assertIn("--disable-breakpad", command)
        self.assertIn("--disable-crash-reporter", command)
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
        self.assertEqual(classify_topic("Заявка на участие в прогнозах АПЛ 2026/2027"), ("registration", "epl"))
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

    def test_probe_rejects_vk_anti_bot_challenge(self) -> None:
        html = "<html><head><title>Проверяем, что вы не робот</title></head></html>"

        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        with self.assertRaises(VkAccessChallengeError):
            probe_public_topic(217130885, 12345678, runner=runner)

    def test_group_discovery_propagates_vk_anti_bot_challenge(self) -> None:
        html = "<html><head><title>Проверяем, что вы не робот</title></head></html>"

        def runner(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        with self.assertRaises(VkAccessChallengeError):
            probe_public_group_topics(217130885, runner=runner)

    def test_shared_lock_serializes_all_vk_probe_types(self) -> None:
        html = '<a href="/topic-217130885_777">EPL predictions</a><div>Arsenal - Chelsea 2:1</div>'
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def runner(command, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(probe_public_topic, 217130885, 1001, runner=runner),
                executor.submit(probe_public_group_topics, 217130885, runner=runner),
                executor.submit(probe_public_topic, 217130885, 1002, runner=runner),
            ]
            for future in futures:
                future.result(timeout=3)

        self.assertEqual(max_active, 1)

    def test_stress_many_sequential_vk_probes_release_the_lock(self) -> None:
        html = "<html><body><div>Arsenal - Chelsea 2:1</div></body></html>"
        calls = 0

        def runner(command, **kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(returncode=0, stdout=html, stderr="")

        for topic_id in range(2000, 2100):
            result = probe_public_topic(217130885, topic_id, runner=runner)
            self.assertEqual(result.score_line_count, 1)

        self.assertEqual(calls, 100)

    def test_many_normal_process_completions_are_reaped(self) -> None:
        for _ in range(20):
            completed = run_chromium_command(
                [sys.executable, "-c", "print('<html><body>ok</body></html>')"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("<html>", completed.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups are used in the production container")
    def test_normal_completion_kills_orphaned_descendants(self) -> None:
        script = (
            "import subprocess, sys; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
            "print(child.pid, flush=True)"
        )
        completed = run_chromium_command(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assert_pid_exits(int(completed.stdout.strip()))

    @unittest.skipUnless(os.name == "posix", "POSIX process groups are used in the production container")
    def test_timeout_kills_browser_and_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            child_pid_path = Path(tmp) / "child.pid"
            script = (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
                "time.sleep(60)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                run_chromium_command(
                    [sys.executable, "-c", script, str(child_pid_path)],
                    capture_output=True,
                    text=True,
                    timeout=0.2,
                )

            self.assertTrue(child_pid_path.exists())
            self.assert_pid_exits(int(child_pid_path.read_text(encoding="ascii")))


if __name__ == "__main__":
    unittest.main()
