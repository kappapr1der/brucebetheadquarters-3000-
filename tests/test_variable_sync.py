from datetime import datetime
import unittest

from brucebet.analytics import match_dossier, player_status_summary
from brucebet.storage import connect, ensure_team, reset_db, upsert_match
from brucebet.variable_sync import (
    canonical_team_name,
    import_fpl_bootstrap,
    sync_match_assessments,
    sync_match_contexts_and_factors,
)


def sample_conn():
    conn = connect(":memory:")
    reset_db(conn)
    upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-21T22:00:00+03:00", None)
    return conn


class VariableSyncTest(unittest.TestCase):
    def test_fpl_bootstrap_imports_player_statuses_for_existing_team_aliases(self) -> None:
        conn = sample_conn()
        payload = {
            "teams": [{"id": 1, "name": "Arsenal"}],
            "element_types": [{"id": 3, "singular_name_short": "MID"}],
            "elements": [
                {
                    "id": 10,
                    "team": 1,
                    "web_name": "Saka",
                    "element_type": 3,
                    "status": "d",
                    "chance_of_playing_next_round": 75,
                    "form": "5.4",
                    "news": "Knock",
                }
            ],
        }

        seen, imported, matched, unmatched = import_fpl_bootstrap(conn, payload, "2026-06-25T10:00:00+00:00")

        self.assertEqual(seen, 1)
        self.assertEqual(imported, 1)
        self.assertEqual(matched, 1)
        self.assertEqual(unmatched, ())
        rows = player_status_summary(conn, "Arsenal")
        self.assertEqual(rows[0]["player"], "Saka")
        self.assertEqual(rows[0]["status"], "doubtful")
        self.assertEqual(rows[0]["availability_pct"], 75)

    def test_contexts_factors_and_assessments_are_generated(self) -> None:
        conn = sample_conn()
        ensure_team(conn, "Arsenal", elo_rating=1840, updated_at="2026-06-25T10:00:00+00:00")
        ensure_team(conn, "Chelsea", elo_rating=1700, updated_at="2026-06-25T10:00:00+00:00")

        contexts, factors, weather_checked, weather_updated, weather_skipped = sync_match_contexts_and_factors(
            conn,
            now=datetime.fromisoformat("2026-08-01T12:00:00+03:00"),
            weather_days=0,
        )
        assessments = sync_match_assessments(
            conn,
            "2026-06-25T10:00:00+00:00",
            now=datetime.fromisoformat("2026-08-01T12:00:00+03:00"),
        )

        self.assertEqual(contexts, 1)
        self.assertEqual(factors, 2)
        self.assertEqual(weather_checked, 0)
        self.assertEqual(weather_updated, 0)
        self.assertEqual(weather_skipped, 0)
        self.assertEqual(assessments, 1)
        dossier = match_dossier(conn, 1)
        self.assertEqual(dossier["context"]["venue"], "Emirates Stadium")
        self.assertIsNotNone(dossier["assessment"]["suggested_score"])
        self.assertGreater(dossier["assessment"]["home_edge"], dossier["assessment"]["away_edge"])

    def test_team_aliases_cover_common_external_names(self) -> None:
        self.assertEqual(canonical_team_name("Brighton & Hove Albion"), "Brighton and Hove Albion")
        self.assertEqual(canonical_team_name("Man Utd"), "Manchester United")
        self.assertEqual(canonical_team_name("Nott'm Forest"), "Nottingham Forest")
        self.assertEqual(canonical_team_name("Spurs"), "Tottenham Hotspur")


if __name__ == "__main__":
    unittest.main()
