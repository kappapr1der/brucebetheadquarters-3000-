from datetime import datetime, timezone
import unittest

from brucebet.football_data_sync import sync_recent_team_form
from brucebet.storage import connect, ensure_team, reset_db


def _match(match_id: int, date: str, home: dict[str, object], away: dict[str, object], home_goals: int, away_goals: int, competition: str) -> dict[str, object]:
    return {
        "id": match_id,
        "utcDate": date,
        "homeTeam": home,
        "awayTeam": away,
        "score": {"fullTime": {"home": home_goals, "away": away_goals}},
        "competition": {"name": competition},
    }


class FootballDataSyncTest(unittest.TestCase):
    def test_form_uses_league_history_then_falls_back_to_team_history(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        ensure_team(conn, "Arsenal")
        ensure_team(conn, "Coventry City")
        ensure_team(conn, "Hull City")
        aliases = {"arsenal fc": "Arsenal", "coventry city": "Coventry City", "hull city": "Hull City"}
        test_case = self

        class FakeClient:
            def competition_teams(self, competition):
                test_case.assertEqual(competition, "PL")
                return [{"id": 1, "name": "Arsenal FC"}, {"id": 2, "name": "Coventry City"}]

            def competition_matches(self, competition, date_from, date_to):
                if competition == "PL":
                    return [
                        _match(index, f"2026-08-{10 + index:02d}T15:00:00Z", {"id": 1, "name": "Arsenal FC"}, {"id": 100 + index, "name": f"League Opponent {index}"}, index % 3, 0, "Premier League")
                        for index in range(1, 6)
                    ]
                test_case.assertEqual(competition, "ELC")
                return [
                    _match(
                        200 + index,
                        f"2026-07-{10 + index:02d}T15:00:00Z",
                        {"id": 300 + index, "name": f"Championship Opponent {index}"},
                        {"id": 3, "name": "Hull City"},
                        index % 2,
                        2,
                        "Championship",
                    )
                    for index in range(1, 6)
                ]

            def team_matches(self, team_id, date_from, date_to, limit):
                test_case.assertEqual(team_id, 2)
                test_case.assertGreaterEqual(limit, 10)
                return [
                    _match(
                        100 + index,
                        f"2026-08-{10 + index:02d}T15:00:00Z",
                        {"id": 2, "name": "Coventry City"},
                        {"id": 200 + index, "name": f"Cup Opponent {index}"},
                        2,
                        index % 2,
                        "Championship",
                    )
                    for index in range(1, 6)
                ]

        result = sync_recent_team_form(
            conn,
            token="test-token",
            active_teams=["Arsenal", "Coventry City", "Hull City"],
            resolve_team=lambda name: aliases.get(name.lower()),
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
            client=FakeClient(),
        )

        self.assertEqual(result.matches_seen, 10)
        self.assertEqual(result.rows_upserted, 15)
        self.assertEqual(result.teams_matched, 3)
        self.assertEqual(result.fallback_teams, ("Coventry City",))
        self.assertEqual(result.unmatched_teams, ())
        rows = conn.execute(
            """
            SELECT t.name AS team, tf.opponent, tf.venue, tf.competition, tf.result, tf.notes
            FROM team_form tf
            JOIN teams t ON t.id = tf.team_id
            ORDER BY t.name, tf.match_date
            """
        ).fetchall()
        self.assertEqual(len(rows), 15)
        arsenal = [row for row in rows if row["team"] == "Arsenal"]
        coventry = [row for row in rows if row["team"] == "Coventry City"]
        hull = [row for row in rows if row["team"] == "Hull City"]
        self.assertEqual(len(arsenal), 5)
        self.assertEqual(len(coventry), 5)
        self.assertEqual(len(hull), 5)
        self.assertTrue(all(row["venue"] == "home" for row in arsenal))
        self.assertTrue(all(row["competition"] == "Championship" for row in coventry))
        self.assertTrue(all(row["venue"] == "away" for row in hull))
        self.assertTrue(all("football-data.org match=" in row["notes"] for row in rows))


if __name__ == "__main__":
    unittest.main()
