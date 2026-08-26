from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from brucebet.glm_analysis import GlmSettings, build_round_brief, build_round_prompt, request_analysis
from brucebet.storage import connect, reset_db, upsert_match, upsert_match_assessment, upsert_match_odds


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class GlmAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "forecasters.sqlite"
        self.conn = connect(self.db_path)
        reset_db(self.conn)
        upsert_match(self.conn, "2", 1, "Arsenal", "Chelsea", "2030-08-21T20:00:00+01:00", None)
        upsert_match_assessment(
            self.conn,
            {
                "round": "2",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.52",
                "home_edge": "0.52",
                "draw_edge": "0.25",
                "away_edge": "0.23",
                "volatility": "0.55",
                "updated_at": "2030-08-20T12:00:00+00:00",
            },
        )
        upsert_match_odds(
            self.conn,
            {
                "round": "2",
                "position": "1",
                "bookmaker": "market_avg",
                "captured_at": "2030-08-20T12:00:00+00:00",
                "home_win": "1.9",
                "draw": "3.7",
                "away_win": "4.0",
            },
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_brief_contains_only_aggregated_round_data(self) -> None:
        round_name, brief = build_round_brief(self.conn, "2")

        self.assertEqual(round_name, "2")
        self.assertEqual(brief["matches"][0]["home"], "Arsenal")
        self.assertEqual(brief["matches"][0]["model"]["suggested_score"], "2:1")
        self.assertEqual(brief["matches"][0]["odds"]["home_win"], 1.9)
        self.assertEqual(brief["matches"][0]["field"]["forecast_rows"], 0)
        prompt = build_round_prompt(brief)
        self.assertIn("Не используй интернет", prompt)
        self.assertIn('"round":"2"', prompt)

    def test_request_uses_general_api_endpoint_and_extracts_text(self) -> None:
        response = FakeResponse({"choices": [{"message": {"content": "Черновик готов."}}]})
        settings = GlmSettings(api_key="secret", base_url="https://api.z.ai/api/paas/v4")

        with patch("brucebet.glm_analysis.urllib.request.urlopen", return_value=response) as urlopen:
            result = request_analysis(settings, "brief")

        self.assertEqual(result, "Черновик готов.")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.z.ai/api/paas/v4/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "glm-4.7-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_timeout_default_allows_for_free_endpoint_queue(self) -> None:
        self.assertEqual(GlmSettings().timeout_seconds, 120)
