import unittest

from brucebet.pl_fixtures import import_pl_fixtures, kickoff_iso
from brucebet.storage import connect, reset_db


def fixture(matchday, millis, home, away, fixture_id=1):
    return {
        "id": fixture_id,
        "gameweek": {"gameweek": float(matchday)},
        "kickoff": {"millis": float(millis)},
        "teams": [
            {"team": {"name": home, "club": {"name": home}}},
            {"team": {"name": away, "club": {"name": away}}},
        ],
        "status": "U",
    }


class PremierLeagueFixturesTest(unittest.TestCase):
    def test_kickoff_iso_converts_public_api_millis_to_moscow(self) -> None:
        item = fixture(1, 1787338800000, "Arsenal", "Coventry City")

        self.assertEqual(kickoff_iso(item), "2026-08-21T22:00:00+03:00")

    def test_import_pl_fixtures_groups_by_gameweek_and_position(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        fixtures = [
            fixture(1, 1787407200000, "Everton", "Crystal Palace", fixture_id=3),
            fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=1),
            fixture(2, 1788012000000, "Liverpool", "Chelsea", fixture_id=4),
        ]

        result = import_pl_fixtures(conn, fixtures)

        self.assertEqual(result.fetched, 3)
        self.assertEqual(result.imported, 3)
        self.assertEqual(result.rounds, 2)
        rows = conn.execute(
            """
            SELECT r.name AS round_name, m.position, m.home, m.away, m.kickoff_at
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            ORDER BY r.sort_order, m.position
            """
        ).fetchall()
        self.assertEqual([row["round_name"] for row in rows], ["1", "1", "2"])
        self.assertEqual(rows[0]["position"], 1)
        self.assertEqual(rows[0]["home"], "Arsenal")
        self.assertEqual(rows[0]["away"], "Coventry City")
        self.assertEqual(rows[0]["kickoff_at"], "2026-08-21T22:00:00+03:00")


if __name__ == "__main__":
    unittest.main()
