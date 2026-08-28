from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from brucebet.odds_api import (
    OddsEvent,
    OddsQuota,
    OddsSnapshot,
    auto_odds_refresh_plan,
    average_event_odds,
    match_pair_key,
    odds_refresh_interval,
    sync_odds_to_db,
)
from brucebet.storage import connect, import_matches, import_teams, reset_db, upsert_match, upsert_match_odds


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_match_sample():
    conn = connect(":memory:")
    reset_db(conn)
    import_teams(conn, EXAMPLES / "teams.csv")
    import_matches(conn, EXAMPLES / "matches.csv")
    return conn


class OddsApiTest(unittest.TestCase):
    def test_auto_refresh_plan_uses_deadline_window_and_snapshot_age(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "2", 1, "Arsenal", "Chelsea", "2026-08-30T19:00:00+00:00", None)
        now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)

        plan = auto_odds_refresh_plan(conn, now=now, lock_minutes=90)
        self.assertTrue(plan.due)
        self.assertEqual(plan.round_name, "2")
        self.assertEqual(plan.reason, "no odds snapshot for target round")
        self.assertEqual(plan.refresh_interval, timedelta(hours=12))

        upsert_match_odds(
            conn,
            {
                "round": "2",
                "position": "1",
                "bookmaker": "market_avg",
                "captured_at": (now - timedelta(hours=2)).isoformat(),
                "home_win": "1.8",
                "draw": "3.5",
                "away_win": "4.2",
            },
        )
        conn.commit()
        fresh = auto_odds_refresh_plan(conn, now=now, lock_minutes=90)
        self.assertFalse(fresh.due)
        self.assertEqual(fresh.reason, "latest target-round snapshot is fresh")

        stale = auto_odds_refresh_plan(conn, now=now + timedelta(hours=11), lock_minutes=90)
        self.assertTrue(stale.due)
        self.assertEqual(stale.reason, "latest target-round snapshot is stale")

    def test_auto_refresh_interval_increases_near_deadline(self) -> None:
        self.assertEqual(odds_refresh_interval(timedelta(hours=50)), timedelta(hours=12))
        self.assertEqual(odds_refresh_interval(timedelta(hours=12)), timedelta(hours=6))
        self.assertEqual(odds_refresh_interval(timedelta(hours=4)), timedelta(hours=2))
        self.assertEqual(odds_refresh_interval(timedelta(minutes=90)), timedelta(minutes=30))

    def test_average_event_odds_handles_h2h_totals_and_btts(self) -> None:
        event = {
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "bookmakers": [
                {
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.8},
                                {"name": "Draw", "price": 3.7},
                                {"name": "Chelsea", "price": 4.2},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 2.5, "price": 1.9},
                                {"name": "Under", "point": 2.5, "price": 1.95},
                            ],
                        },
                        {
                            "key": "btts",
                            "outcomes": [
                                {"name": "Yes", "price": 1.72},
                                {"name": "No", "price": 2.1},
                            ],
                        },
                    ]
                },
                {
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Arsenal", "price": 1.9},
                                {"name": "Draw", "price": 3.5},
                                {"name": "Chelsea", "price": 4.0},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "point": 2.5, "price": 1.8},
                                {"name": "Under", "point": 2.5, "price": 2.05},
                            ],
                        },
                    ]
                },
            ],
        }

        snapshot = average_event_odds(event)

        self.assertEqual(snapshot.bookmaker_count, 2)
        self.assertEqual(snapshot.home_win, 1.85)
        self.assertEqual(snapshot.draw, 3.6)
        self.assertEqual(snapshot.away_win, 4.1)
        self.assertEqual(snapshot.over_2_5, 1.85)
        self.assertEqual(snapshot.under_2_5, 2.0)
        self.assertEqual(snapshot.btts_yes, 1.72)
        self.assertEqual(snapshot.btts_no, 2.1)

    def test_epl_aliases_normalize_api_team_names(self) -> None:
        self.assertEqual(match_pair_key("Brighton & Hove Albion", "Newcastle United"), match_pair_key("Brighton", "Newcastle"))
        self.assertEqual(match_pair_key("Tottenham Hotspur", "Manchester United"), match_pair_key("Tottenham", "Manchester United"))

    def test_sync_odds_to_db_matches_aliases_and_imports_snapshot(self) -> None:
        conn = load_match_sample()
        requested: dict[str, object] = {}

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key

            def odds(self, **kwargs):
                requested.update(kwargs)
                return (
                    [
                        OddsEvent(
                            event_id="evt-1",
                            sport_key="soccer_epl",
                            commence_time="2026-08-16T13:00:00Z",
                            home_team="Brighton & Hove Albion",
                            away_team="Newcastle United",
                            snapshot=OddsSnapshot(home_win=2.6, draw=3.4, away_win=2.75, over_2_5=1.82, under_2_5=2.05, bookmaker_count=9),
                            raw_bookmaker_count=9,
                        )
                    ],
                    OddsQuota(requests_remaining=499, requests_used=1, requests_last=1),
                )

        with patch("brucebet.odds_api.TheOddsApiClient", FakeClient):
            result = sync_odds_to_db(conn, api_key="test-key", captured_at="2026-06-25T08:00:00+00:00")

        self.assertEqual(result.events_seen, 1)
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.inserted, 1)
        self.assertRegex(str(requested["commence_time_from"]), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(str(requested["commence_time_to"]), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        row = conn.execute(
            """
            SELECT mo.*, m.home, m.away
            FROM match_odds mo
            JOIN matches m ON m.id = mo.match_id
            WHERE mo.bookmaker = 'market_avg'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["home"], "Brighton")
        self.assertEqual(row["away"], "Newcastle")
        self.assertEqual(row["home_win"], 2.6)
        self.assertEqual(row["draw"], 3.4)
        self.assertEqual(row["away_win"], 2.75)
        self.assertIn("evt-1", row["notes"])


if __name__ == "__main__":
    unittest.main()
