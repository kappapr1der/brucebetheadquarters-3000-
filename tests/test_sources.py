import unittest

from brucebet.sources import SourceCheck, SourceConfig, check_api_football


class SourcesTest(unittest.TestCase):
    def test_missing_key_reports_unconfigured_source(self) -> None:
        item = check_api_football(SourceConfig(api_football_key=""))

        self.assertIsInstance(item, SourceCheck)
        self.assertEqual(item.name, "API-Football")
        self.assertFalse(item.ok)
        self.assertFalse(item.configured)

    def test_thesportsdb_defaults_to_free_key(self) -> None:
        config = SourceConfig()

        self.assertEqual(config.thesportsdb_key, "123")


if __name__ == "__main__":
    unittest.main()
