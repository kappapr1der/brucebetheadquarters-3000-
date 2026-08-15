from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerLifecycleTests(unittest.TestCase):
    def test_brucebet_service_runs_with_init(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        brucebet_block = compose.split("  vk-oauth:", 1)[0]

        self.assertIn("  brucebet:\n", brucebet_block)
        self.assertIn("    init: true\n", brucebet_block)
        self.assertNotIn("pids_limit", brucebet_block)


if __name__ == "__main__":
    unittest.main()
