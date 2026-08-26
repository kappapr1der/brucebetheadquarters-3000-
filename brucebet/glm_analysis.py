from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
import urllib.error
import urllib.request

from .analytics import field_summary, match_rows_for_round, target_round_name


USER_AGENT = "BruceBetHQ/0.1 (+https://github.com/kappapr1der/brucebetheadquarters-3000-)"
DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"


class GlmAnalysisError(RuntimeError):
    """Raised when Z.ai cannot return a usable auxiliary analysis."""


@dataclass(frozen=True)
class GlmSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: int = 120
    max_tokens: int = 700

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


def _latest_odds(conn: sqlite3.Connection, match_id: int) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT bookmaker, captured_at, home_win, draw, away_win,
               over_2_5, under_2_5, btts_yes, btts_no
        FROM match_odds
        WHERE match_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (match_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _assessment(conn: sqlite3.Connection, match_id: int) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT suggested_score, risk_level, confidence, home_edge, draw_edge,
               away_edge, volatility, updated_at
        FROM match_assessments
        WHERE match_id = ?
        """,
        (match_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def build_round_brief(conn: sqlite3.Connection, round_name: str | None = None) -> tuple[str, dict[str, object]]:
    """Produce an auditable, read-only context for an external LLM call."""

    resolved_round = round_name or target_round_name(conn)
    if not resolved_round:
        raise GlmAnalysisError("Не найден активный тур.")
    matches = match_rows_for_round(conn, resolved_round)
    if not matches:
        raise GlmAnalysisError(f"В туре {resolved_round} нет матчей.")

    payload_matches: list[dict[str, object]] = []
    for match in matches:
        summary = field_summary(conn, int(match["id"]))
        outcomes = dict(summary["outcomes"])
        scores = dict(summary["scores"])
        payload_matches.append(
            {
                "position": int(match["position"]),
                "home": str(match["home"]),
                "away": str(match["away"]),
                "kickoff_at": match["kickoff_at"],
                "model": _assessment(conn, int(match["id"])),
                "odds": _latest_odds(conn, int(match["id"])),
                "field": {
                    "outcomes": outcomes,
                    "scores": scores,
                    "forecast_rows": sum(outcomes.values()),
                },
            }
        )
    return str(resolved_round), {
        "round": str(resolved_round),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matches": payload_matches,
    }


def build_round_prompt(brief: dict[str, object]) -> str:
    """Make the model a constrained second opinion, never a source of facts."""

    source = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(
        [
            "Ты вспомогательный футбольный аналитик для конкурса прогнозов счетов АПЛ.",
            "Работай ТОЛЬКО с JSON ниже. Не используй интернет и не выдумывай травмы, форму, составы, коэффициенты или прогнозы участников.",
            "Модель и рынок являются входными сигналами, а не доказанной истиной. Твой ответ - независимый черновик, он не отправляется во VK и не меняет прогноз пользователя.",
            "Ответь по-русски, без таблиц, максимум 3200 символов.",
            "Структура ответа: 'Картина тура' (2-3 короткие строки), 'По матчам' (по одной строке на матч: #, счет, уверенность высокая/средняя/низкая, краткая причина), 'Риски' (до 4 матчей), 'Перед дедлайном проверить' (до 3 пунктов).",
            "Если у матча нет данных поля, прямо скажи, что стратегия против поля пока не определена.",
            "JSON:",
            source,
        ]
    )


def request_analysis(settings: GlmSettings, prompt: str) -> str:
    if not settings.configured:
        raise GlmAnalysisError("GLM_API_KEY не задан.")
    base_url = settings.base_url.rstrip("/")
    if not base_url.startswith("https://"):
        raise GlmAnalysisError("GLM_BASE_URL должен начинаться с https://")
    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": "Ты аккуратный аналитик. Не выдавай предположения за факты."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # The auxiliary round summary is a constrained synthesis task. Turning
        # off chain-of-thought keeps the free Flash endpoint responsive.
        "thinking": {"type": "disabled"},
        "max_tokens": settings.max_tokens,
    }
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru",
            "Authorization": f"Bearer {settings.api_key.strip()}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300].replace("\n", " ")
        raise GlmAnalysisError(f"Z.ai вернул HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GlmAnalysisError(f"Не удалось подключиться к Z.ai: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GlmAnalysisError("Z.ai не ответил вовремя.") from exc

    try:
        response_payload = json.loads(body)
        content = response_payload["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise GlmAnalysisError("Z.ai вернул ответ в неожиданном формате.") from exc
    if not isinstance(content, str) or not content.strip():
        raise GlmAnalysisError("Z.ai не вернул текст анализа.")
    return content.strip()
