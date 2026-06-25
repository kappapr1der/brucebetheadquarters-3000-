from pathlib import Path
import unittest

from brucebet.analytics import hq_summary, risk_map, strategy_summary
from brucebet.storage import (
    connect,
    init_db,
    import_absences,
    import_match_assessments,
    import_match_contexts,
    import_match_odds,
    import_matches,
    import_participants,
    import_predictions,
    import_team_form,
    import_team_match_factors,
    import_teams,
    reset_db,
    upsert_match,
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_epl_sample():
    conn = connect(":memory:")
    reset_db(conn)
    import_participants(conn, EXAMPLES / "participants.csv")
    import_teams(conn, EXAMPLES / "teams.csv")
    import_matches(conn, EXAMPLES / "matches.csv")
    import_predictions(conn, EXAMPLES / "predictions.csv")
    import_team_form(conn, EXAMPLES / "team_form.csv")
    import_absences(conn, EXAMPLES / "absences.csv")
    import_match_contexts(conn, EXAMPLES / "match_contexts.csv")
    import_match_odds(conn, EXAMPLES / "match_odds.csv")
    import_team_match_factors(conn, EXAMPLES / "team_match_factors.csv")
    import_match_assessments(conn, EXAMPLES / "match_assessments.csv")
    return conn


class EplHeadquartersTest(unittest.TestCase):
    def test_hq_summary_uses_active_epl_season(self) -> None:
        conn = load_epl_sample()
        item = hq_summary(conn, user_participant="Bruce Wayne")

        self.assertEqual(item["season"]["competition_code"], "epl")
        self.assertEqual(item["round_name"], "1")
        self.assertEqual(item["match_count"], 4)
        self.assertEqual(item["participant_count"], 5)
        self.assertEqual(item["paid_count"], 4)
        self.assertEqual(item["bank_rub"], 1200)
        self.assertEqual(item["predictions"]["mine"], 4)

    def test_risk_map_splits_safe_and_risk_matches(self) -> None:
        conn = load_epl_sample()
        item = risk_map(conn)

        self.assertEqual(item["round_name"], "1")
        self.assertEqual([row["label"] for row in item["safe"]], ["Liverpool - Burnley"])
        self.assertIn("Brighton - Newcastle", [row["label"] for row in item["risk"]])
        self.assertIn("Tottenham - Manchester United", [row["label"] for row in item["risk"]])

    def test_strategy_knows_bruce_when_sample_uses_full_name(self) -> None:
        conn = load_epl_sample()
        item = strategy_summary(conn, user_participant="Bruce Wayne")

        self.assertEqual(item["mode"], "protect")
        self.assertEqual(item["me"].name, "Bruce Wayne")
        self.assertEqual(item["gap"], 0)

    def test_old_round_unique_constraint_is_migrated_to_seasons(self) -> None:
        conn = connect(":memory:")
        conn.execute(
            """
            CREATE TABLE rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL,
                deadline_at TEXT
            )
            """
        )
        conn.execute("INSERT INTO rounds(id, name, sort_order) VALUES(1, '1', 1)")

        init_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Chelsea", "2026-08-15T14:30:00+03:00", None)

        row = conn.execute("SELECT COUNT(*) AS count FROM rounds WHERE name = '1'").fetchone()
        self.assertEqual(row["count"], 2)

        legacy = conn.execute(
            """
            SELECT c.code
            FROM rounds r
            JOIN seasons s ON s.id = r.season_id
            JOIN competitions c ON c.id = s.competition_id
            WHERE r.id = 1
            """
        ).fetchone()
        self.assertEqual(legacy["code"], "legacy")


if __name__ == "__main__":
    unittest.main()
