import unittest

from brucebet.pl_fixtures import FixtureSyncError, import_pl_fixtures, kickoff_iso
from brucebet.storage import (
    FixtureIdentityError,
    connect,
    ensure_team,
    init_db,
    reset_db,
    table_columns,
    upsert_match,
    upsert_prediction,
)


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

    def test_init_db_adds_fixture_identity_columns_to_legacy_matches(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        conn.execute("DROP INDEX matches_source_fixture_unique")
        conn.execute("ALTER TABLE matches DROP COLUMN source_fixture_id")
        conn.execute("ALTER TABLE matches DROP COLUMN source")

        init_db(conn)

        self.assertTrue({"source", "source_fixture_id"}.issubset(table_columns(conn, "matches")))
        indexes = [row["name"] for row in conn.execute("PRAGMA index_list(matches)")]
        self.assertIn("matches_source_fixture_unique", indexes)

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

        identities = conn.execute(
            "SELECT source, source_fixture_id FROM matches ORDER BY source_fixture_id"
        ).fetchall()
        self.assertEqual([row["source"] for row in identities], ["premierleague.com"] * 3)
        self.assertEqual([row["source_fixture_id"] for row in identities], ["1", "3", "4"])
        self.assertEqual(result.created, 3)
        self.assertEqual(result.unmatched, 0)

    def test_kickoff_reorder_preserves_match_id_prediction_and_factors(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        first = [
            fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101),
            fixture(1, 1787342400000, "Everton", "Crystal Palace", fixture_id=102),
        ]
        import_pl_fixtures(conn, first)
        original = conn.execute(
            "SELECT id, position FROM matches WHERE source_fixture_id = '101'"
        ).fetchone()
        original_id = int(original["id"])
        upsert_prediction(conn, "Bruce Wayne", "1", 1, "1:0", "2026-08-15T12:00:00+03:00", "test")
        arsenal_id = ensure_team(conn, "Arsenal")
        coventry_id = ensure_team(conn, "Coventry City")
        conn.executemany(
            "INSERT INTO team_match_factors(match_id, team_id, side) VALUES(?, ?, ?)",
            [(original_id, arsenal_id, "home"), (original_id, coventry_id, "away")],
        )
        conn.commit()

        reordered = [
            fixture(1, 1787346000000, "Arsenal", "Coventry City", fixture_id=101),
            fixture(1, 1787335200000, "Everton", "Crystal Palace", fixture_id=102),
        ]
        result = import_pl_fixtures(conn, reordered)

        current = conn.execute(
            "SELECT id, position, home, away FROM matches WHERE source_fixture_id = '101'"
        ).fetchone()
        self.assertEqual(int(current["id"]), original_id)
        self.assertEqual(int(current["position"]), 2)
        self.assertEqual((current["home"], current["away"]), ("Arsenal", "Coventry City"))
        self.assertEqual(conn.execute("SELECT match_id FROM predictions").fetchone()[0], original_id)
        factor_ids = {
            row[0]
            for row in conn.execute(
                "SELECT team_id FROM team_match_factors WHERE match_id = ?",
                (original_id,),
            )
        }
        self.assertEqual(factor_ids, {arsenal_id, coventry_id})
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.stale_factors_removed, 0)

    def test_repeated_fixture_sync_is_semantically_idempotent(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        fixtures = [
            fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101),
            fixture(1, 1787342400000, "Everton", "Crystal Palace", fixture_id=102),
        ]
        import_pl_fixtures(conn, fixtures)
        repeated = import_pl_fixtures(conn, fixtures)

        self.assertEqual(repeated.created, 0)
        self.assertEqual(repeated.updated, 0)
        self.assertEqual(repeated.moved, 0)
        self.assertEqual(repeated.unmatched, 0)
        self.assertEqual(repeated.stale_factors_removed, 0)
        self.assertEqual(repeated.before_hash, repeated.after_hash)
        ledger = conn.execute(
            "SELECT status, created, updated, moved, unmatched, before_hash, after_hash "
            "FROM fixture_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ledger["status"], "success")
        self.assertEqual(tuple(ledger[key] for key in ("created", "updated", "moved", "unmatched")), (0, 0, 0, 0))
        self.assertEqual(ledger["before_hash"], ledger["after_hash"])

    def test_ambiguous_legacy_migration_rolls_back_atomically(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        upsert_match(conn, "1", 1, "Arsenal", "Coventry City", "2026-08-21T22:00:00+03:00", None)
        upsert_match(conn, "1", 2, "Everton", "Crystal Palace", "2026-08-22T17:00:00+03:00", None)
        conn.commit()
        before = [tuple(row) for row in conn.execute("SELECT id, position, home, away, source, source_fixture_id FROM matches ORDER BY id")]

        with self.assertRaises(FixtureSyncError):
            import_pl_fixtures(
                conn,
                [fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101)],
            )

        after = [tuple(row) for row in conn.execute("SELECT id, position, home, away, source, source_fixture_id FROM matches ORDER BY id")]
        self.assertEqual(after, before)
        ledger = conn.execute(
            "SELECT status, before_hash, after_hash FROM fixture_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ledger["status"], "failed")
        self.assertEqual(ledger["before_hash"], ledger["after_hash"])

    def test_unfinished_fixture_sync_preserves_manual_result(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        original_id = upsert_match(
            conn,
            "1",
            1,
            "Arsenal",
            "Coventry City",
            "2026-08-21T22:00:00+03:00",
            "2:1",
        )
        conn.commit()

        import_pl_fixtures(
            conn,
            [fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101)],
        )

        row = conn.execute("SELECT id, result FROM matches").fetchone()
        self.assertEqual(int(row["id"]), original_id)
        self.assertEqual(row["result"], "2:1")

    def test_migration_removes_only_stale_team_factors(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        match_id = upsert_match(
            conn,
            "1",
            1,
            "Arsenal",
            "Coventry City",
            "2026-08-21T22:00:00+03:00",
            None,
        )
        team_ids = {name: ensure_team(conn, name) for name in ("Arsenal", "Coventry City", "Everton", "Chelsea")}
        conn.executemany(
            "INSERT INTO team_match_factors(match_id, team_id, side) VALUES(?, ?, ?)",
            [
                (match_id, team_ids["Arsenal"], "home"),
                (match_id, team_ids["Coventry City"], "away"),
                (match_id, team_ids["Everton"], "home"),
                (match_id, team_ids["Chelsea"], "away"),
            ],
        )
        conn.commit()

        result = import_pl_fixtures(
            conn,
            [fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101)],
        )

        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT t.name FROM team_match_factors f JOIN teams t ON t.id = f.team_id"
            )
        }
        self.assertEqual(remaining, {"Arsenal", "Coventry City"})
        self.assertEqual(result.stale_factors_removed, 2)

    def test_stable_source_id_rejects_team_pair_change(self) -> None:
        conn = connect(":memory:")
        reset_db(conn)
        first = [fixture(1, 1787338800000, "Arsenal", "Coventry City", fixture_id=101)]
        initial = import_pl_fixtures(conn, first)

        with self.assertRaises(FixtureIdentityError):
            upsert_match(
                conn,
                "1",
                1,
                "Everton",
                "Chelsea",
                "2026-08-21T22:00:00+03:00",
                None,
            )

        with self.assertRaises(FixtureSyncError):
            import_pl_fixtures(
                conn,
                [fixture(1, 1787338800000, "Everton", "Chelsea", fixture_id=101)],
            )

        row = conn.execute("SELECT home, away, position FROM matches WHERE source_fixture_id = '101'").fetchone()
        self.assertEqual((row["home"], row["away"], row["position"]), ("Arsenal", "Coventry City", 1))
        ledger = conn.execute(
            "SELECT status, before_hash, after_hash FROM fixture_sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ledger["status"], "failed")
        self.assertEqual(ledger["before_hash"], initial.after_hash)
        self.assertEqual(ledger["before_hash"], ledger["after_hash"])


if __name__ == "__main__":
    unittest.main()
