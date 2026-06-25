import csv
import json
from pathlib import Path
import tempfile
import unittest

from brucebet.snapshot import export_snapshot
from brucebet.storage import (
    connect,
    import_absences,
    import_match_assessments,
    import_match_contexts,
    import_match_odds,
    import_matches,
    import_participants,
    import_player_statuses,
    import_predictions,
    import_team_form,
    import_team_match_factors,
    import_teams,
    reset_db,
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_sample():
    conn = connect(":memory:")
    reset_db(conn)
    import_participants(conn, EXAMPLES / "participants.csv")
    import_teams(conn, EXAMPLES / "teams.csv")
    import_matches(conn, EXAMPLES / "matches.csv")
    import_predictions(conn, EXAMPLES / "predictions.csv")
    import_team_form(conn, EXAMPLES / "team_form.csv")
    import_absences(conn, EXAMPLES / "absences.csv")
    import_player_statuses(conn, EXAMPLES / "player_statuses.csv")
    import_match_contexts(conn, EXAMPLES / "match_contexts.csv")
    import_match_odds(conn, EXAMPLES / "match_odds.csv")
    import_team_match_factors(conn, EXAMPLES / "team_match_factors.csv")
    import_match_assessments(conn, EXAMPLES / "match_assessments.csv")
    return conn


class SnapshotTest(unittest.TestCase):
    def test_export_snapshot_writes_safe_stable_files(self) -> None:
        conn = load_sample()
        with tempfile.TemporaryDirectory() as tmp:
            result = export_snapshot(conn, tmp, label="unit")
            out_dir = Path(tmp)

            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["label"], "unit")
            self.assertEqual(manifest["scope"], "active_season")
            self.assertEqual(manifest["competition_code"], "epl")
            self.assertEqual(manifest["tables"]["matches.csv"], 4)
            self.assertEqual(result.tables["predictions.csv"], 20)

            with (out_dir / "predictions.csv").open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["round"], "1")
            self.assertIn("participant", rows[0])
            self.assertIn("score", rows[0])

            filenames = {path.name for path in out_dir.iterdir()}
            self.assertIn("manifest.json", filenames)
            self.assertIn("match_assessments.csv", filenames)
            self.assertFalse(any(path.suffix == ".sqlite" for path in out_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
