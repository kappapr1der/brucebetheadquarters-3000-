from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from brucebet.glm_analysis import GlmAnalysisError, GlmSettings, build_round_brief, build_round_prompt, request_analysis
from brucebet.storage import (
    connect,
    reset_db,
    upsert_absence,
    upsert_match,
    upsert_match_assessment,
    upsert_match_context,
    upsert_match_odds,
    upsert_team_form,
    upsert_team_match_factor,
)


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
        upsert_team_form(
            self.conn,
            {
                "team": "Arsenal",
                "match_date": "2030-08-14",
                "opponent": "Leeds",
                "venue": "home",
                "goals_for": "2",
                "goals_against": "0",
                "xg_for": "1.8",
                "xg_against": "0.6",
                "result": "W",
            },
        )
        upsert_absence(
            self.conn,
            {
                "team": "Chelsea",
                "player": "Example Player",
                "status": "injured",
                "impact_rating": "0.8",
                "updated_at": "2030-08-20T12:00:00+00:00",
            },
        )
        upsert_match_context(
            self.conn,
            {
                "round": "2",
                "position": "1",
                "home_rest_days": "6",
                "away_rest_days": "3",
                "weather": "clear",
            },
        )
        upsert_team_match_factor(
            self.conn,
            {
                "round": "2",
                "position": "1",
                "team": "Arsenal",
                "side": "home",
                "fatigue": "0.1",
                "morale": "0.7",
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
        self.assertEqual(brief["matches"][0]["context"]["home_rest_days"], 6)
        self.assertEqual(brief["matches"][0]["form"]["home_last_5"][0]["result"], "W")
        self.assertEqual(brief["matches"][0]["absences"][0]["player"], "Example Player")
        self.assertEqual(brief["matches"][0]["factors"][0]["fatigue"], 0.1)
        self.assertEqual(brief["matches"][0]["field"]["forecast_rows"], 0)
        prompt = build_round_prompt(brief)
        self.assertIn("Нельзя использовать интернет", prompt)
        self.assertIn("Не пиши общих вступлений", prompt)
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

    def test_analysis_presentation_counts_only_actual_alternatives_and_limits_checklist(self) -> None:
        response = FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "\n".join(
                                [
                                    "Вывод: 0 матчей поддерживают базовый счет, 2 спорных.",
                                    "Матчи:",
                                    "#1 Arsenal - Chelsea: 2:1; база 2:1; опора: away_edge 0.2",
                                    "#2 Leeds - Everton: 1:0; база 0:1; опора: volatility 0.7",
                                    "Перед дедлайном проверить:",
                                    "форма: данных нет",
                                    "травмы: данных нет",
                                    "мораль: данных нет",
                                    "прессинг: данных нет",
                                ]
                            )
                        }
                    }
                ]
            }
        )

        with patch("brucebet.glm_analysis.urllib.request.urlopen", return_value=response):
            result = request_analysis(GlmSettings(api_key="secret"), "brief")

        self.assertIn("Вывод: подтверждают базовый счет: 1; предлагают альтернативу: 1", result)
        self.assertIn("преимущество гостей 0.2", result)
        self.assertIn("нестабильность 0.7", result)
        self.assertNotIn("прессинг: данных нет", result)

    def test_request_retries_once_with_free_fallback_after_rate_limit(self) -> None:
        overloaded = urllib.error.HTTPError(
            "https://api.z.ai/api/paas/v4/chat/completions",
            429,
            "Too many requests",
            None,
            io.BytesIO(b'{"error":{"code":"1305"}}'),
        )
        response = FakeResponse({"choices": [{"message": {"content": "Черновик готов."}}]})
        settings = GlmSettings(api_key="secret")

        with patch(
            "brucebet.glm_analysis.urllib.request.urlopen",
            side_effect=[overloaded, response],
        ) as urlopen:
            result = request_analysis(settings, "brief")

        self.assertEqual(result, "Черновик готов.")
        models = [
            json.loads(call.args[0].data.decode("utf-8"))["model"]
            for call in urlopen.call_args_list
        ]
        self.assertEqual(models, ["glm-4.7-flash", "glm-4.5-flash"])

    def test_rate_limit_message_is_russian_after_both_free_models_reject(self) -> None:
        overloaded = lambda: urllib.error.HTTPError(
            "https://api.z.ai/api/paas/v4/chat/completions",
            429,
            "Too many requests",
            None,
            io.BytesIO(b'{"error":{"code":"1305"}}'),
        )
        settings = GlmSettings(api_key="secret")

        with patch(
            "brucebet.glm_analysis.urllib.request.urlopen",
            side_effect=[overloaded(), overloaded()],
        ):
            with self.assertRaisesRegex(GlmAnalysisError, "Бесплатные модели Z.ai"):
                request_analysis(settings, "brief")
