from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
import urllib.error
import urllib.request

from .analytics import field_summary, match_rows_for_round, target_round_name


USER_AGENT = "BruceBetHQ/0.1 (+https://github.com/kappapr1der/brucebetheadquarters-3000-)"
DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"
DEFAULT_FALLBACK_MODEL = "glm-4.5-flash"

_MATCH_LINE_RE = re.compile(
    r"^(#\d+\s+.+?:\s*)(?P<pick>\d+:\d+);\s*база\s+(?P<base>\d+:\d+|нет данных);",
    re.IGNORECASE,
)
_RUSSIAN_LABELS = {
    "away_edge": "преимущество гостей",
    "home_edge": "преимущество хозяев",
    "draw_edge": "вероятность ничьей",
    "volatility": "нестабильность",
    "confidence": "уверенность модели",
    "away_win": "коэффициент победы гостей",
    "home_win": "коэффициент победы хозяев",
    "morale": "мораль",
    "tactical_fit": "тактический фактор",
    "pressing_advantage": "прессинг",
    "set_piece_edge": "стандарты",
}


class GlmAnalysisError(RuntimeError):
    """Raised when Z.ai cannot return a usable auxiliary analysis."""


class GlmRateLimitError(GlmAnalysisError):
    """Raised when a model is temporarily unavailable because its queue is full."""


@dataclass(frozen=True)
class GlmSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
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


def _match_context(conn: sqlite3.Connection, match_id: int) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT home_rest_days, away_rest_days, home_travel_km, away_travel_km,
               weather, temperature_c, pitch, referee, home_motivation,
               away_motivation, home_rotation_risk, away_rotation_risk, notes
        FROM match_contexts
        WHERE match_id = ?
        """,
        (match_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _team_factors(conn: sqlite3.Connection, match_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT f.side, f.expected_lineup_confidence, f.absences_impact,
               f.fatigue, f.morale, f.tactical_fit, f.pressing_advantage,
               f.set_piece_edge, f.motivation, f.notes
        FROM team_match_factors f
        WHERE f.match_id = ?
        ORDER BY f.side
        """,
        (match_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _team_form(conn: sqlite3.Connection, team_name: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT form.match_date, form.opponent, form.venue, form.competition,
               form.goals_for, form.goals_against, form.xg_for, form.xg_against,
               form.result, form.importance
        FROM team_form form
        JOIN teams team ON team.id = form.team_id
        WHERE team.name = ?
        ORDER BY form.match_date DESC, form.id DESC
        LIMIT 5
        """,
        (team_name,),
    ).fetchall()
    return [dict(row) for row in rows]


def _absences(conn: sqlite3.Connection, home: str, away: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT team.name AS team, absence.player, absence.role, absence.status,
               absence.severity, absence.impact_rating, absence.expected_return,
               absence.source, absence.updated_at
        FROM absences absence
        JOIN teams team ON team.id = absence.team_id
        WHERE team.name IN (?, ?)
        ORDER BY team.name, absence.impact_rating DESC, absence.player
        """,
        (home, away),
    ).fetchall()
    return [dict(row) for row in rows]


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
                "context": _match_context(conn, int(match["id"])),
                "factors": _team_factors(conn, int(match["id"])),
                "form": {
                    "home_last_5": _team_form(conn, str(match["home"])),
                    "away_last_5": _team_form(conn, str(match["away"])),
                },
                "absences": _absences(conn, str(match["home"]), str(match["away"])),
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
    """Make the model a disciplined second opinion, never a source of facts."""

    source = json.dumps(brief, ensure_ascii=False, separators=(",", ":"))
    return "\n".join(
        [
            "Ты - строгий аудитор данных для конкурса прогнозов счетов АПЛ.",
            "Работай ТОЛЬКО с JSON ниже. Нельзя использовать интернет, общие знания о футболе или придумывать форму, травмы, составы, коэффициенты, мотивацию, календарь и прогнозы участников.",
            "Не пиши общих вступлений: запретны фразы о лидерах, аутсайдерах, привлекательности тура, вероятном ходе матча и 'явном преимуществе', если это не подтверждено конкретными полями JSON.",
            "Каждое объяснение должно называть минимум один конкретный сигнал из JSON: счет базовой модели, вероятности модели, коэффициенты, форму, травму/доступность, отдых, контекст, фактор команды или агрегированное поле. Если подходящего сигнала нет, пиши 'данных нет'.",
            "Модель и рынок - входные сигналы, не доказанная истина. При несогласии с базовым счетом модели прямо укажи исходный и свой счет. Не выдумывай причину несогласия.",
            "Ответь только по-русски: названия команд можно оставить как в JSON, но не используй иероглифы и слова на других языках. Без таблиц, максимум 3200 символов.",
            "Строгий формат ответа:",
            "Вывод: одна строка о том, сколько матчей подтверждают базовый счет, сколько предлагают альтернативу и сколько не имеют базового счета. Считай строго по строкам ниже: одинаковый итоговый и базовый счет - подтверждение; другой счет - альтернатива. Нельзя назвать матч спорным, если итоговый счет равен базе.",
            "Матчи: ровно одна строка на каждый матч: '#<номер> <хозяева> - <гости>: <итоговый счет>; база <счет модели или нет данных>; уверенность <высокая/средняя/низкая>; опора: <конкретные сигналы>'. Названия технических полей переводи на русский: away_edge - преимущество гостей, home_edge - преимущество хозяев, volatility - нестабильность, confidence - уверенность модели.",
            "Риски: до четырех строк только с номером матча и фактической причиной риска из JSON: высокая volatility, низкая confidence, разнобой поля, отсутствие/устаревание данных или заметные кадровые/контекстные факторы.",
            "Перед дедлайном проверить: максимум три строки только о реально отсутствующих либо устаревших полях JSON. Однотипные пробелы обязательно объединяй в одну строку: например, форму, травмы и составы. Не советуй проверять то, чего JSON уже не содержит и не называй новые факты.",
            "Если у матча нет прогнозов поля, прямо укажи: 'поле: данных нет'. Не раскрывай имена участников: доступны только агрегаты поля.",
            "JSON:",
            source,
        ]
    )


def _normalize_analysis(content: str) -> str:
    """Enforce compact, internally consistent presentation around LLM text."""

    for source_label, russian_label in _RUSSIAN_LABELS.items():
        content = re.sub(rf"\b{re.escape(source_label)}\b", russian_label, content)

    lines = content.splitlines()
    supported = alternatives = missing_base = 0
    for line in lines:
        match = _MATCH_LINE_RE.match(line.strip())
        if match is None:
            continue
        base = match.group("base").lower()
        if base == "нет данных":
            missing_base += 1
        elif match.group("pick") == base:
            supported += 1
        else:
            alternatives += 1
    if supported + alternatives + missing_base:
        for index, line in enumerate(lines):
            if line.strip().lower().startswith("вывод:"):
                lines[index] = (
                    f"Вывод: подтверждают базовый счет: {supported}; "
                    f"предлагают альтернативу: {alternatives}; без базового счета: {missing_base}."
                )
                break

    deadline_index = next(
        (index for index, line in enumerate(lines) if line.strip().lower().startswith("перед дедлайном проверить:")),
        None,
    )
    if deadline_index is not None:
        kept: list[str] = []
        nonempty_count = 0
        for line in lines[deadline_index + 1 :]:
            if line.strip():
                nonempty_count += 1
                if nonempty_count > 3:
                    continue
            kept.append(line)
        lines = lines[: deadline_index + 1] + kept
    return "\n".join(lines).strip()


def _request_model(settings: GlmSettings, prompt: str, model: str) -> str:
    """Call one model once; caller decides whether a 429 deserves a fallback."""

    base_url = settings.base_url.rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты аккуратный русскоязычный аудитор данных. "
                    "Не выдавай предположения за факты и не добавляй сведения вне входного JSON."
                ),
            },
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
        if exc.code == 429:
            raise GlmRateLimitError("Бесплатная очередь Z.ai сейчас перегружена.") from exc
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
    return _normalize_analysis(content)


def request_analysis(settings: GlmSettings, prompt: str) -> str:
    if not settings.configured:
        raise GlmAnalysisError("GLM_API_KEY не задан.")
    if not settings.base_url.rstrip("/").startswith("https://"):
        raise GlmAnalysisError("GLM_BASE_URL должен начинаться с https://")

    try:
        return _request_model(settings, prompt, settings.model)
    except GlmRateLimitError as primary_error:
        fallback = settings.fallback_model.strip()
        if not fallback or fallback == settings.model:
            raise GlmAnalysisError(
                "Бесплатная очередь Z.ai сейчас перегружена. Попробуй снова через 5-10 минут."
            ) from primary_error
        try:
            return _request_model(settings, prompt, fallback)
        except GlmRateLimitError as fallback_error:
            raise GlmAnalysisError(
                "Бесплатные модели Z.ai сейчас перегружены. Попробуй снова через 5-10 минут."
            ) from fallback_error
