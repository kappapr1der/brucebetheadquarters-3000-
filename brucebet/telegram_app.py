from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable, Iterable

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .analytics import (
    calendar_matches,
    compare_participants,
    compute_standings,
    field_summary,
    hq_summary,
    match_dossier,
    match_header,
    next_calendar_match,
    player_status_summary,
    prediction_views_for_match,
    recommend_match,
    risk_map,
    round_deadlines,
    strategy_summary,
)
from .odds_api import (
    DEFAULT_ODDS_BOOKMAKER,
    DEFAULT_ODDS_MARKETS,
    DEFAULT_ODDS_REGIONS,
    DEFAULT_ODDS_SPORT,
    OddsApiError,
    TheOddsApiClient,
    sync_odds_to_db,
)
from .pl_fixtures import DEFAULT_PL_COMPSEASON_ID, DEFAULT_PL_SEASON_LABEL, PremierLeagueApiError, sync_pl_fixtures_to_db
from .scoring import is_standard_score, normalize_score, parse_datetime, parse_score
from .service_messages import (
    deadline_after_message,
    deadline_reminder_schedule,
    deadline_schedule_created,
    participant_forecasts_loaded,
    render,
)
from .sources import SourceConfig, check_all_sources
from .storage import (
    activate_profile,
    active_season,
    connect,
    find_match,
    import_matches,
    import_participants,
    import_predictions,
    init_db,
)
from .variable_sync import VariableSyncResult, sync_match_variables
from .vk_parser import parse_file as parse_vk_file


LOGGER = logging.getLogger(__name__)
MAX_MESSAGE = 3900


@dataclass(frozen=True)
class BotSettings:
    token: str
    db_path: Path
    data_dir: Path
    user_participant: str
    competition: str
    season: str
    season_display: str
    allowed_chat_ids: frozenset[int]
    lock_minutes: int
    odds_api_key: str
    odds_sport: str
    odds_regions: str
    odds_markets: str
    odds_bookmaker: str
    odds_days_ahead: int
    api_football_key: str
    football_data_token: str
    thesportsdb_key: str
    pl_compseason_id: int
    pl_season_label: str
    timezone: str
    variables_days_ahead: int
    weather_days_ahead: int
    auto_sync_enabled: bool
    auto_sync_interval_hours: int
    auto_sync_first_delay_minutes: int


def parse_chat_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


def parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> BotSettings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    return BotSettings(
        token=token,
        db_path=Path(os.getenv("BRUCEBET_DB_PATH", "data/forecasters.sqlite")),
        data_dir=Path(os.getenv("BRUCEBET_DATA_DIR", "data")),
        user_participant=os.getenv("BRUCEBET_USER_PARTICIPANT", "Bruce Wayne"),
        competition=os.getenv("BRUCEBET_COMPETITION", "epl"),
        season=os.getenv("BRUCEBET_SEASON", "2026/27"),
        season_display=os.getenv("BRUCEBET_SEASON_DISPLAY", "EPL 2026/27"),
        allowed_chat_ids=parse_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")),
        lock_minutes=int(os.getenv("BRUCEBET_LOCK_MINUTES", "90")),
        odds_api_key=os.getenv("THE_ODDS_API_KEY", "").strip(),
        odds_sport=os.getenv("THE_ODDS_API_SPORT", DEFAULT_ODDS_SPORT).strip() or DEFAULT_ODDS_SPORT,
        odds_regions=os.getenv("THE_ODDS_API_REGIONS", DEFAULT_ODDS_REGIONS).strip() or DEFAULT_ODDS_REGIONS,
        odds_markets=os.getenv("THE_ODDS_API_MARKETS", DEFAULT_ODDS_MARKETS).strip() or DEFAULT_ODDS_MARKETS,
        odds_bookmaker=os.getenv("THE_ODDS_API_BOOKMAKER", DEFAULT_ODDS_BOOKMAKER).strip() or DEFAULT_ODDS_BOOKMAKER,
        odds_days_ahead=int(os.getenv("THE_ODDS_API_DAYS_AHEAD", "30")),
        api_football_key=os.getenv("API_FOOTBALL_KEY", "").strip(),
        football_data_token=os.getenv("FOOTBALL_DATA_TOKEN", "").strip(),
        thesportsdb_key=os.getenv("THESPORTSDB_KEY", "123").strip() or "123",
        pl_compseason_id=int(os.getenv("PREMIER_LEAGUE_COMPSEASON_ID", str(DEFAULT_PL_COMPSEASON_ID))),
        pl_season_label=os.getenv("PREMIER_LEAGUE_SEASON_LABEL", DEFAULT_PL_SEASON_LABEL).strip() or DEFAULT_PL_SEASON_LABEL,
        timezone=os.getenv("BRUCEBET_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        variables_days_ahead=int(os.getenv("BRUCEBET_VARIABLE_DAYS_AHEAD", "365")),
        weather_days_ahead=int(os.getenv("BRUCEBET_WEATHER_DAYS_AHEAD", "16")),
        auto_sync_enabled=parse_bool(os.getenv("BRUCEBET_AUTO_SYNC"), default=True),
        auto_sync_interval_hours=int(os.getenv("BRUCEBET_AUTO_SYNC_INTERVAL_HOURS", "12")),
        auto_sync_first_delay_minutes=int(os.getenv("BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES", "5")),
    )


def settings_from_context(context: ContextTypes.DEFAULT_TYPE) -> BotSettings:
    return context.application.bot_data["settings"]


def conn_from_context(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    settings = settings_from_context(context)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    init_db(conn)
    activate_profile(
        conn,
        competition_code=settings.competition,
        season_name=settings.season,
        season_display_name=settings.season_display,
        lock_minutes=settings.lock_minutes,
    )
    return conn


async def send_text(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    chunks = [text[index : index + MAX_MESSAGE] for index in range(0, len(text), MAX_MESSAGE)] or [text]
    for chunk in chunks:
        await update.effective_message.reply_text(chunk)


def require_access(func: Callable) -> Callable:
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings = settings_from_context(context)
        chat = update.effective_chat
        if settings.allowed_chat_ids and (chat is None or chat.id not in settings.allowed_chat_ids):
            LOGGER.warning("Rejected chat_id=%s", chat.id if chat else None)
            if update.effective_message:
                await update.effective_message.reply_text("Нет доступа к этому боту.")
            return
        return await func(update, context)

    return wrapper


def query_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip()


def render_rows(headers: list[str], rows: list[list[object]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * width for width in widths])]
    lines.extend(fmt.format(*[str(value) for value in row]) for row in rows)
    return "```\n" + "\n".join(lines) + "\n```"


def render_risk_map(item: dict[str, object]) -> str:
    sections: list[str] = [f"Тур: {clean(item.get('round_name'))}"]
    labels = [("safe", "Безопасные"), ("slippery", "Скользкие"), ("risk", "Матчи для риска"), ("unknown", "Без поля")]
    for key, title in labels:
        rows = [
            [
                row["position"],
                row["label"],
                row["top_outcome"],
                row["top_share"],
                row["predictions"],
                row["suggested_score"],
            ]
            for row in item.get(key, [])
        ]
        sections.append(title + ":\n" + render_rows(["#", "match", "top", "share", "n", "base"], rows))
    return "\n\n".join(sections)


def render_calendar(items: list[object]) -> str:
    return render_rows(
        ["round", "#", "match", "kickoff", "deadline", "status", "mine", "field"],
        [
            [
                item.round_name,
                item.position,
                item.label,
                clean(item.kickoff_at.isoformat() if item.kickoff_at else None),
                clean(item.deadline_at.isoformat() if item.deadline_at else None),
                item.status,
                "yes" if item.my_prediction_count else "no",
                item.prediction_count,
            ]
            for item in items
        ],
    )


def clean(value: object) -> str:
    return "" if value is None else str(value)


def render_key_values(items: list[tuple[str, object]]) -> str:
    rows = [[key, clean(value)] for key, value in items if clean(value) != ""]
    return render_rows(["field", "value"], rows)


def render_odds_quota(remaining: int | None, used: int | None, last: int | None) -> str:
    return render_rows(
        ["metric", "value"],
        [
            ["requests_remaining", clean(remaining)],
            ["requests_used", clean(used)],
            ["requests_last", clean(last)],
        ],
    )


def source_config_from_settings(settings: BotSettings, timeout: int = 20) -> SourceConfig:
    return SourceConfig(
        the_odds_api_key=settings.odds_api_key,
        api_football_key=settings.api_football_key,
        football_data_token=settings.football_data_token,
        thesportsdb_key=settings.thesportsdb_key,
        timeout=timeout,
    )


def render_source_checks(checks: list[object]) -> str:
    return render_rows(
        ["source", "ok", "configured", "detail"],
        [[item.name, "yes" if item.ok else "no", "yes" if item.configured else "no", item.detail] for item in checks],
    )


def render_stored_odds(dossier: dict[str, object], limit: int = 10) -> str:
    match = dossier["match"]
    odds = dossier["odds"]
    if not odds:
        return match_header(match) + "\n\nКэфов по этому матчу пока нет."
    rows = [
        [
            row["bookmaker"],
            row["captured_at"],
            clean(row["home_win"]),
            clean(row["draw"]),
            clean(row["away_win"]),
            clean(row["under_2_5"]),
            clean(row["over_2_5"]),
            clean(row["btts_yes"]),
            clean(row["btts_no"]),
        ]
        for row in odds[:limit]
    ]
    return match_header(match) + "\n\n" + render_rows(["book", "captured", "home", "draw", "away", "u2.5", "o2.5", "btts_y", "btts_n"], rows)


def render_variable_sync(result: VariableSyncResult) -> str:
    rows: list[tuple[str, object]] = [
        ("updated_at", result.updated_at),
        ("fpl_players_seen", result.fpl_players_seen),
        ("fpl_players_imported", result.fpl_players_imported),
        ("fpl_teams_matched", result.fpl_teams_matched),
        ("elo_teams_checked", result.elo_teams_checked),
        ("elo_teams_updated", result.elo_teams_updated),
        ("contexts_upserted", result.contexts_upserted),
        ("factors_upserted", result.factors_upserted),
        ("weather_checked", result.weather_checked),
        ("weather_updated", result.weather_updated),
        ("weather_skipped", result.weather_skipped),
        ("assessments_upserted", result.assessments_upserted),
    ]
    if result.fpl_unmatched_teams:
        rows.append(("fpl_unmatched_teams", ", ".join(result.fpl_unmatched_teams[:10])))
    if result.elo_unmatched_teams:
        rows.append(("elo_unmatched_teams", ", ".join(result.elo_unmatched_teams[:10])))
    if result.errors:
        rows.append(("errors", " | ".join(result.errors)))
    return "Variables sync done.\n\n" + render_key_values(rows)


def render_dossier(dossier: dict[str, object]) -> str:
    sections = [match_header(dossier["match"])]
    sections.append(
        "Teams:\n"
        + render_rows(
            ["side", "team", "elo", "attack", "defense"],
            [
                [
                    "home",
                    dossier["home"]["name"],
                    clean(dossier["home"]["elo_rating"]),
                    clean(dossier["home"]["attack_rating"]),
                    clean(dossier["home"]["defense_rating"]),
                ],
                [
                    "away",
                    dossier["away"]["name"],
                    clean(dossier["away"]["elo_rating"]),
                    clean(dossier["away"]["attack_rating"]),
                    clean(dossier["away"]["defense_rating"]),
                ],
            ],
        )
    )
    if dossier["assessment"]:
        item = dossier["assessment"]
        sections.append(
            "Model draft:\n"
            + render_key_values(
                [
                    ("suggested_score", item["suggested_score"]),
                    ("risk_level", item["risk_level"]),
                    ("confidence", item["confidence"]),
                    ("home_edge", item["home_edge"]),
                    ("draw_edge", item["draw_edge"]),
                    ("away_edge", item["away_edge"]),
                    ("volatility", item["volatility"]),
                    ("note", item["consensus_note"]),
                ]
            )
        )
    if dossier["context"]:
        ctx = dossier["context"]
        sections.append(
            "Context:\n"
            + render_key_values(
                [
                    ("venue", ctx["venue"]),
                    ("city", ctx["city"]),
                    ("home_rest_days", ctx["home_rest_days"]),
                    ("away_rest_days", ctx["away_rest_days"]),
                    ("weather", ctx["weather"]),
                    ("temperature_c", ctx["temperature_c"]),
                    ("notes", ctx["notes"]),
                ]
            )
        )
    if dossier["factors"]:
        sections.append(
            "Team factors:\n"
            + render_rows(
                ["side", "team", "lineup", "absences", "fatigue", "motivation"],
                [
                    [
                        row["side"],
                        row["team"],
                        clean(row["expected_lineup_confidence"]),
                        clean(row["absences_impact"]),
                        clean(row["fatigue"]),
                        clean(row["motivation"]),
                    ]
                    for row in dossier["factors"]
                ],
            )
        )
    if dossier["absences"]:
        sections.append(
            "Absences:\n"
            + render_rows(
                ["team", "player", "role", "status", "impact"],
                [
                    [row["team"], row["player"], clean(row["role"]), row["status"], clean(row["impact_rating"])]
                    for row in dossier["absences"][:20]
                ],
            )
        )
    if dossier["odds"]:
        sections.append(
            "Odds:\n"
            + render_rows(
                ["book", "captured", "home", "draw", "away", "u2.5", "o2.5"],
                [
                    [
                        row["bookmaker"],
                        row["captured_at"],
                        clean(row["home_win"]),
                        clean(row["draw"]),
                        clean(row["away_win"]),
                        clean(row["under_2_5"]),
                        clean(row["over_2_5"]),
                    ]
                    for row in dossier["odds"][:5]
                ],
            )
        )
    return "\n\n".join(sections)


def db_status(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM participants) AS participants,
            (SELECT COUNT(*) FROM rounds) AS rounds,
            (SELECT COUNT(*) FROM matches) AS matches,
            (SELECT COUNT(*) FROM predictions) AS predictions
        """
    ).fetchone()
    return (
        f"База: участников {row['participants']}, туров {row['rounds']}, "
        f"матчей {row['matches']}, прогнозов {row['predictions']}."
    )


@require_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    season = active_season(conn)
    lines = [
        "BruceBet 3000 на связи.",
        f"Активный профиль: {season['display_name'] or season['name']}.",
        db_status(conn),
        f"Твой участник: {settings.user_participant}.",
        "",
        "Команды: /hq, /calendar, /today, /week, /next, /round, /variables, /load, /table, /field, /recommend, /odds, /quota, /sources, /sync_fixtures, /sync_odds, /risk, /strategy, /match, /vs, /deadlines, /schedule, /audit, /id.",
    ]
    lines.append("New: /sync_variables, /dossier <match>.")
    if not settings.allowed_chat_ids:
        lines.append("")
        if update.effective_chat is not None:
            lines.append(f"Текущий chat_id для TELEGRAM_ALLOWED_CHAT_IDS: {update.effective_chat.id}")
        lines.append("Внимание: TELEGRAM_ALLOWED_CHAT_IDS не задан, бот открыт для любого чата с токеном.")
    await send_text(update, "\n".join(lines))


@require_access
async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        await send_text(update, "Не вижу chat_id в этом апдейте.")
        return

    lines = [
        f"chat_id: {chat.id}",
        f"chat_type: {chat.type}",
    ]
    if chat.title:
        lines.append(f"chat_title: {chat.title}")
    if user is not None:
        lines.append(f"user_id: {user.id}")
        if user.username:
            lines.append(f"username: @{user.username}")
    lines.append("")
    lines.append("Для whitelist в .env:")
    lines.append(f"TELEGRAM_ALLOWED_CHAT_IDS={chat.id}")
    await send_text(update, "\n".join(lines))


@require_access
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(
        update,
        "\n".join(
            [
                "/sync_variables - sync FPL, ClubElo, context, weather, model variables",
                "/dossier <match> - match variable card",
                "/id - показать chat_id для whitelist",
                "/load - пришли VK-пасту текстом или файлом",
                "/hq - штаб активного тура",
                "/table - таблица конкурса",
                "/field <матч> - поле прогнозов",
                "/recommend <матч> - рекомендация по матчу",
                "/odds <матч> - последние сохранённые кэфы",
                "/quota - остаток кредитов The Odds API",
                "/sources - проверить все источники данных",
                "/sync_fixtures - подтянуть официальный календарь PL",
                "/sync_odds - подтянуть кэфы The Odds API в базу",
                "/risk [тур] - риск-карта тура",
                "/strategy - стратегия относительно таблицы",
                "/calendar - календарь ближайших матчей",
                "/today - матчи сегодня",
                "/week - матчи на 7 дней",
                "/next - следующий матч",
                "/round <тур> - календарь тура",
                "/variables [команда] - форма/статус игроков",
                "/match <матч> - прогнозы участников",
                "/vs <участник> - отличия тебя от участника",
                "/deadlines - дедлайны туров",
                "/schedule - поставить напоминания в этот чат",
                "/audit - пропуски, нестандартные форматы, поздние проверки",
            ]
        ),
    )


@require_access
async def load_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(
        update,
        "Кидай VK-пасту текстом или .txt файлом. Я распарсю туры, прогнозы участников, обновлю базу и отвечу отчётом.",
    )


@require_access
async def table_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    season = active_season(conn)
    rows = [
        [
            item.rank,
            item.name,
            item.total,
            item.exact_hits,
            item.diff_hits,
            item.outcome_hits,
            item.late,
            "yes" if item.paid else "no",
            item.prize_rub,
        ]
        for item in compute_standings(conn, entry_fee_rub=int(season["entry_fee_rub"]), lock_minutes=settings.lock_minutes)
    ]
    await send_text(update, render_rows(["#", "name", "pts", "exact", "diff", "outcome", "late", "paid", "prize"], rows))


@require_access
async def hq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    item = hq_summary(conn, user_participant=settings.user_participant, lock_minutes=settings.lock_minutes)
    season = item["season"]
    deadline = item["deadline"]
    effective = deadline.effective_deadline_at.isoformat() if deadline and deadline.effective_deadline_at else "не указан"
    focus = item["risk"].get("risk", [])[:3] + item["risk"].get("slippery", [])[:3]
    lines = [
        f"Штаб: {season['display_name'] or season['name']}",
        f"Тур: {item['round_name'] or 'не найден'}",
        f"Дедлайн: {effective}",
        f"Матчей: {item['match_count']}",
        f"Твой прогноз: {item['predictions']['mine']}/{item['match_count']}",
        f"Прогнозы поля: {item['predictions']['participants']}/{item['participant_count']} участников, строк {item['predictions']['rows']}",
        f"Участников с взносом: {item['paid_count']}/{item['participant_count']}. Банк: {item['bank_rub']} руб.",
        "",
        "Фокус риска:",
        render_rows(
            ["#", "match", "top", "share", "base"],
            [[row["position"], row["label"], row["top_outcome"], row["top_share"], row["suggested_score"]] for row in focus],
        ),
    ]
    await send_text(update, "\n".join(lines))


@require_access
async def risk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = conn_from_context(context)
    await send_text(update, render_risk_map(risk_map(conn, query_text(context) or None)))


@require_access
async def strategy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    item = strategy_summary(conn, user_participant=settings.user_participant, lock_minutes=settings.lock_minutes)
    me = item["me"]
    leader = item["leader"]
    lines = [
        f"Режим: {item['mode']}",
        f"Ты: {me.rank if me else '?'} место, {me.total if me else '?'} очков",
        f"Лидер: {leader.name if leader else '?'} ({leader.total if leader else '?'} очков)",
        f"Отставание: {item['gap'] if item['gap'] is not None else '?'}",
        "",
        item["advice"],
        "",
        render_risk_map(item["risk"]),
    ]
    await send_text(update, "\n".join(lines))


@require_access
async def field_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = query_text(context)
    if not query:
        await send_text(update, "Напиши матч: /field Бельгия")
        return
    conn = conn_from_context(context)
    match = find_match(conn, query)
    summary = field_summary(conn, int(match["id"]))
    text = [
        match_header(match),
        "",
        "Outcomes:",
        render_rows(["outcome", "count"], [[key, value] for key, value in summary["outcomes"].most_common()]),
        "",
        "Scores:",
        render_rows(["score", "count"], [[key, value] for key, value in summary["scores"].most_common()]),
    ]
    await send_text(update, "\n".join(text))


@require_access
async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = query_text(context)
    if not query:
        await send_text(update, "Напиши матч: /recommend Бельгия")
        return
    conn = conn_from_context(context)
    match = find_match(conn, query)
    item = recommend_match(conn, int(match["id"]))
    lines = [
        match_header(item["match"]),
        f"База: {item['suggested_score']}",
        f"Риск: {item['risk_level']}",
        f"Уверенность: {item['confidence']}",
        f"Доля главного исхода: {item['top_outcome_share']}",
    ]
    if item["consensus_note"]:
        lines.append(f"Поле: {item['consensus_note']}")
    if item["contrarian_note"]:
        lines.append(f"Контр-сценарий: {item['contrarian_note']}")
    lines.extend(
        [
            "",
            "Исходы:",
            render_rows(["outcome", "count"], [[key, value] for key, value in item["outcomes"].most_common()]),
            "",
            "Популярные счета:",
            render_rows(["score", "count"], [[key, value] for key, value in item["scores"].most_common(8)]),
        ]
    )
    await send_text(update, "\n".join(lines))


@require_access
async def odds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = query_text(context)
    if not query:
        await send_text(update, "Напиши матч: /odds Arsenal")
        return
    conn = conn_from_context(context)
    match = find_match(conn, query)
    await send_text(update, render_stored_odds(match_dossier(conn, int(match["id"]))))


@require_access
async def quota_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    if not settings.odds_api_key:
        await send_text(update, "THE_ODDS_API_KEY не задан в .env.")
        return
    try:
        check = await asyncio.to_thread(TheOddsApiClient(settings.odds_api_key).sports)
    except OddsApiError as exc:
        await send_text(update, f"The Odds API не ответил нормально:\n{exc}")
        return
    lines = [
        "The Odds API:",
        f"sports_count: {check.sports_count}",
        f"sport {settings.odds_sport}: {'есть' if settings.odds_sport in check.sport_keys else 'не найден'}",
        "",
        render_odds_quota(check.quota.requests_remaining, check.quota.requests_used, check.quota.requests_last),
    ]
    await send_text(update, "\n".join(lines))


@require_access
async def sources_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    await send_text(update, "Проверяю источники данных. Это может занять несколько секунд.")
    checks = await asyncio.to_thread(check_all_sources, source_config_from_settings(settings))
    ok_count = sum(1 for item in checks if item.ok)
    lines = [
        f"Источники: {ok_count}/{len(checks)} живы.",
        "",
        render_source_checks(checks),
    ]
    await send_text(update, "\n".join(lines))


def sync_fixtures_worker(settings: BotSettings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        activate_profile(
            conn,
            competition_code=settings.competition,
            season_name=settings.season,
            season_display_name=settings.season_display,
            lock_minutes=settings.lock_minutes,
        )
        return sync_pl_fixtures_to_db(
            conn,
            compseason_id=settings.pl_compseason_id,
            season_label=settings.pl_season_label,
            timezone_name=settings.timezone,
        )
    finally:
        conn.close()


@require_access
async def sync_fixtures_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    await send_text(update, f"Тяну официальный календарь PL {settings.pl_season_label}.")
    try:
        result = await asyncio.to_thread(sync_fixtures_worker, settings)
    except PremierLeagueApiError as exc:
        await send_text(update, f"Календарь не синкнулся:\n{exc}")
        return
    lines = [
        "Календарь загружен.",
        f"source: {result.source}",
        f"compseason_id: {result.compseason_id}",
        f"fetched: {result.fetched}",
        f"imported: {result.imported}",
        f"rounds: {result.rounds}",
        f"first_kickoff: {result.first_kickoff}",
        f"last_kickoff: {result.last_kickoff}",
    ]
    await send_text(update, "\n".join(lines))


def sync_odds_worker(settings: BotSettings):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        activate_profile(
            conn,
            competition_code=settings.competition,
            season_name=settings.season,
            season_display_name=settings.season_display,
            lock_minutes=settings.lock_minutes,
        )
        return sync_odds_to_db(
            conn,
            api_key=settings.odds_api_key,
            sport=settings.odds_sport,
            regions=settings.odds_regions,
            markets=settings.odds_markets,
            bookmaker=settings.odds_bookmaker,
            days_ahead=settings.odds_days_ahead,
        )
    finally:
        conn.close()


@require_access
async def sync_odds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    if not settings.odds_api_key:
        await send_text(update, "THE_ODDS_API_KEY не задан в .env.")
        return
    await send_text(update, f"Тяну кэфы: {settings.odds_sport}, {settings.odds_regions}, {settings.odds_markets}.")
    try:
        result = await asyncio.to_thread(sync_odds_worker, settings)
    except OddsApiError as exc:
        await send_text(update, f"The Odds API не синкнулся:\n{exc}")
        return
    lines = [
        "Синк кэфов готов.",
        f"events_seen: {result.events_seen}",
        f"matched: {result.matched}",
        f"inserted: {result.inserted}",
        f"unmatched: {len(result.unmatched)}",
        f"captured_at: {result.captured_at}",
        "",
        render_odds_quota(result.quota.requests_remaining, result.quota.requests_used, result.quota.requests_last),
    ]
    if result.unmatched:
        lines.append("")
        lines.append("Не сматчил:")
        lines.extend(f"- {item}" for item in result.unmatched[:10])
    await send_text(update, "\n".join(lines))


def sync_variables_worker(settings: BotSettings) -> VariableSyncResult:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        activate_profile(
            conn,
            competition_code=settings.competition,
            season_name=settings.season,
            season_display_name=settings.season_display,
            lock_minutes=settings.lock_minutes,
        )
        return sync_match_variables(
            conn,
            days_ahead=settings.variables_days_ahead,
            weather_days=settings.weather_days_ahead,
            timezone_name=settings.timezone,
        )
    finally:
        conn.close()


@require_access
async def sync_variables_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    await send_text(update, "Syncing FPL, ClubElo, context, weather, and model draft variables.")
    result = await asyncio.to_thread(sync_variables_worker, settings)
    await send_text(update, render_variable_sync(result))


@require_access
async def match_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    query = query_text(context)
    if not query:
        await send_text(update, "Напиши матч: /match Бельгия")
        return
    conn = conn_from_context(context)
    match = find_match(conn, query)
    views = prediction_views_for_match(conn, int(match["id"]), lock_minutes=settings.lock_minutes)
    rows = [[view.participant, view.score, view.category, view.points] for view in views]
    await send_text(update, match_header(match) + "\n\n" + render_rows(["participant", "score", "category", "pts"], rows))


@require_access
async def dossier_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = query_text(context)
    if not query:
        await send_text(update, "Usage: /dossier Arsenal")
        return
    conn = conn_from_context(context)
    match = find_match(conn, query)
    await send_text(update, render_dossier(match_dossier(conn, int(match["id"]))))


@require_access
async def vs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    opponent = query_text(context)
    if not opponent:
        await send_text(update, "Напиши участника: /vs Игорь Григорьев")
        return
    conn = conn_from_context(context)
    comparison = compare_participants(conn, settings.user_participant, opponent, lock_minutes=settings.lock_minutes)
    rows = [
        [
            row["round"],
            row["position"],
            row["match"],
            row["result"],
            row["mine"],
            row["opponent"],
            "" if row["delta"] is None else row["delta"],
        ]
        for row in comparison
    ]
    await send_text(update, render_rows(["round", "#", "match", "result", settings.user_participant, opponent, "delta"], rows))


def audit_text(conn: sqlite3.Connection, lock_minutes: int) -> str:
    season_id = int(active_season(conn)["id"])
    missing = list(
        conn.execute(
            """
            SELECT p.name AS participant, r.name AS round_name, GROUP_CONCAT(m.position, ',') AS positions, COUNT(*) AS count
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            CROSS JOIN matches m
            JOIN rounds r ON r.id = m.round_id
            LEFT JOIN predictions pr ON pr.participant_id = p.id AND pr.match_id = m.id
            WHERE sp.season_id = ?
              AND sp.active = 1
              AND r.season_id = ?
              AND pr.id IS NULL
            GROUP BY p.id, r.id
            ORDER BY r.sort_order, p.name
            """,
            (season_id, season_id),
        )
    )
    score_rows = list(
        conn.execute(
            """
            SELECT p.name AS participant, r.name AS round_name, pr.score, m.position
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            ORDER BY r.sort_order, p.name, m.position
            """,
            (season_id,),
        )
    )
    unreadable: dict[tuple[str, str], list[str]] = {}
    nonstandard: dict[tuple[str, str], list[str]] = {}
    for row in score_rows:
        score = parse_score(row["score"])
        key = (row["round_name"], row["participant"])
        if score is None:
            unreadable.setdefault(key, []).append(f"{row['position']}={row['score']}")
        elif not is_standard_score(row["score"]):
            nonstandard.setdefault(key, []).append(f"{row['position']}={row['score']}->{normalize_score(row['score'])}")

    late_rows = list(
        conn.execute(
            """
            SELECT p.name AS participant, r.name AS round_name, r.deadline_at AS round_deadline_at,
                   m.position, m.kickoff_at, pr.submitted_at
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
            ORDER BY r.sort_order, p.name, m.position
            """,
            (season_id,),
        )
    )
    from .analytics import prediction_is_eligible

    late: dict[tuple[str, str], list[str]] = {}
    for row in late_rows:
        if prediction_is_eligible(
            parse_datetime(row["submitted_at"]),
            parse_datetime(row["kickoff_at"]),
            parse_datetime(row["round_deadline_at"]),
            lock_minutes,
        ):
            continue
        late.setdefault((row["round_name"], row["participant"]), []).append(str(row["position"]))

    lines = ["Audit"]
    lines.append("\nMissing:")
    lines.append(render_rows(["round", "participant", "count", "positions"], [[r["round_name"], r["participant"], r["count"], r["positions"]] for r in missing]))
    lines.append("\nUnreadable:")
    lines.append(render_rows(["round", "participant", "count", "examples"], [[k[0], k[1], len(v), "; ".join(v[:4])] for k, v in sorted(unreadable.items())]))
    lines.append("\nNon-standard but accepted:")
    lines.append(render_rows(["round", "participant", "count", "examples"], [[k[0], k[1], len(v), "; ".join(v[:4])] for k, v in sorted(nonstandard.items())]))
    lines.append("\nLate / needs kickoff check:")
    lines.append(render_rows(["round", "participant", "count", "positions"], [[k[0], k[1], len(v), ",".join(v)] for k, v in sorted(late.items())]))
    return "\n".join(lines)


@require_access
async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    await send_text(update, audit_text(conn, settings.lock_minutes))


@require_access
async def deadlines_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    rows = []
    for item in round_deadlines(conn, lock_minutes=settings.lock_minutes):
        rows.append(
            [
                item.round_name,
                clean(item.first_kickoff_at.isoformat() if item.first_kickoff_at else None),
                clean(item.stored_deadline_at.isoformat() if item.stored_deadline_at else None),
                clean(item.computed_deadline_at.isoformat() if item.computed_deadline_at else None),
                clean(item.effective_deadline_at.isoformat() if item.effective_deadline_at else None),
            ]
        )
    await send_text(update, render_rows(["round", "first_kickoff", "stored", "computed", "effective"], rows))


@require_access
async def calendar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    await send_text(
        update,
        render_calendar(
            calendar_matches(
                conn,
                days=14,
                user_participant=settings.user_participant,
                lock_minutes=settings.lock_minutes,
            )
        ),
    )


@require_access
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    await send_text(
        update,
        render_calendar(
            calendar_matches(
                conn,
                days=1,
                user_participant=settings.user_participant,
                lock_minutes=settings.lock_minutes,
                start_at=today,
            )
        ),
    )


@require_access
async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    await send_text(
        update,
        render_calendar(
            calendar_matches(
                conn,
                days=7,
                user_participant=settings.user_participant,
                lock_minutes=settings.lock_minutes,
            )
        ),
    )


@require_access
async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    conn = conn_from_context(context)
    item = next_calendar_match(conn, user_participant=settings.user_participant, lock_minutes=settings.lock_minutes)
    if item is None:
        await send_text(update, "Upcoming matches with kickoff_at not found.")
        return
    await send_text(update, render_calendar([item]))


@require_access
async def round_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    round_name = query_text(context)
    if not round_name:
        await send_text(update, "Usage: /round 1")
        return
    conn = conn_from_context(context)
    start = datetime(1900, 1, 1).astimezone()
    await send_text(
        update,
        render_calendar(
            calendar_matches(
                conn,
                days=60000,
                user_participant=settings.user_participant,
                lock_minutes=settings.lock_minutes,
                round_name=round_name,
                start_at=start,
                include_unknown_kickoff=True,
            )
        ),
    )


@require_access
async def variables_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = query_text(context) or None
    conn = conn_from_context(context)
    rows = player_status_summary(conn, query)
    await send_text(
        update,
        render_rows(
            ["team", "player", "role", "status", "avail", "form", "source", "updated"],
            [
                [
                    row["team"],
                    row["player"],
                    clean(row["role"]),
                    clean(row["status"]),
                    clean(row["availability_pct"]),
                    clean(row["form_rating"]),
                    clean(row["source"]),
                    clean(row["updated_at"]),
                ]
                for row in rows
            ],
        ),
    )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    await context.bot.send_message(chat_id=data["chat_id"], text=data["text"])


@require_access
async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    chat = update.effective_chat
    if chat is None:
        return
    if context.job_queue is None:
        await send_text(update, "JobQueue не подключён. Проверь зависимость python-telegram-bot[job-queue].")
        return

    now = datetime.now().astimezone()
    conn = conn_from_context(context)
    scheduled = 0
    messages: list[str] = []
    for deadline in round_deadlines(conn, lock_minutes=settings.lock_minutes):
        effective = deadline.effective_deadline_at
        if effective is None or effective <= now:
            continue
        for item in deadline_reminder_schedule(effective):
            if item.send_at <= now:
                continue
            context.job_queue.run_once(
                reminder_job,
                when=item.send_at,
                data={"chat_id": chat.id, "text": item.reply.text},
                name=f"round-{deadline.round_name}-{item.key}-{chat.id}",
            )
            scheduled += 1
        context.job_queue.run_once(
            reminder_job,
            when=effective,
            data={"chat_id": chat.id, "text": deadline_after_message(effective).text},
            name=f"round-{deadline.round_name}-deadline-passed-{chat.id}",
        )
        scheduled += 1
        messages.append(render(deadline_schedule_created(deadline.round_name, effective)))

    if scheduled == 0:
        await send_text(update, "Будущих дедлайнов с датой не нашёл. Нужен шаблон тура или kickoff/deadline в базе.")
    else:
        await send_text(update, "\n\n".join(messages) + f"\n\nВсего задач напоминаний: {scheduled}.")


def import_parsed_files(settings: BotSettings, templates_count: int) -> tuple[int, int, int]:
    conn = connect(settings.db_path)
    init_db(conn)
    activate_profile(
        conn,
        competition_code=settings.competition,
        season_name=settings.season,
        season_display_name=settings.season_display,
        lock_minutes=settings.lock_minutes,
    )
    participants_path = settings.data_dir / "participants.csv"
    matches_path = settings.data_dir / "vk_matches.csv"
    predictions_path = settings.data_dir / "vk_predictions.csv"
    participants = import_participants(conn, participants_path) if participants_path.exists() else 0
    matches = import_matches(conn, matches_path) if matches_path.exists() else 0
    predictions = import_predictions(conn, predictions_path) if predictions_path.exists() else 0
    return participants, matches, predictions


def count_nonstandard_predictions(path: Path) -> tuple[int, int]:
    unreadable = 0
    nonstandard = 0
    if not path.exists():
        return unreadable, nonstandard
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            score = row.get("score")
            if parse_score(score) is None:
                unreadable += 1
            elif not is_standard_score(score):
                nonstandard += 1
    return unreadable, nonstandard


async def process_vk_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    settings = settings_from_context(context)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
        handle.write(text)
        source = Path(handle.name)
    try:
        templates, records = await asyncio.to_thread(parse_vk_file, source, settings.data_dir)
        participants, matches, predictions = await asyncio.to_thread(import_parsed_files, settings, len(templates))
        unreadable, nonstandard = count_nonstandard_predictions(settings.data_dir / "vk_predictions.csv")
    finally:
        try:
            source.unlink()
        except OSError:
            pass

    round_names = ", ".join(template.round_name for template in templates)
    participants_by_round = {
        template.round_name: len({record.participant for record in records if record.round_name == template.round_name})
        for template in templates
    }
    reply = [
        f"Принято. Распарсил туры: {round_names or 'не нашёл'}",
        f"Матчей: {sum(len(template.matches) for template in templates)}. Строк прогнозов: {len(records)}.",
        f"Импорт: participants={participants}, matches={matches}, predictions={predictions}.",
        f"Участники по турам: {participants_by_round}.",
    ]
    reply.extend(
        item.text
        for item in participant_forecasts_loaded(
            round_name=round_names or "?",
            participants_count=max(participants_by_round.values()) if participants_by_round else 0,
            prediction_rows=len(records),
            invalid_rows=unreadable,
            nonstandard_rows=nonstandard,
        )
    )
    await send_text(update, "\n".join(reply))


@require_access
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text if update.effective_message else ""
    if "Шаблон" in text and "Дедлайн" in text:
        await process_vk_text(update, context, text)
    else:
        await send_text(update, "Не понял формат. Если это тур/прогнозы, пришли VK-пасту с заголовком `Шаблон ... Дедлайн ...`.")


@require_access
async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None or update.effective_message.document is None:
        return
    document = update.effective_message.document
    if not (document.file_name or "").lower().endswith(".txt"):
        await send_text(update, "Пока принимаю .txt с VK-пастой.")
        return
    tg_file = await document.get_file()
    raw = await tg_file.download_as_bytearray()
    text = raw.decode("utf-8-sig")
    await process_vk_text(update, context, text)


def auto_sync_worker(settings: BotSettings) -> tuple[object, VariableSyncResult]:
    fixture_result = sync_fixtures_worker(settings)
    variable_result = sync_variables_worker(settings)
    return fixture_result, variable_result


async def auto_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = settings_from_context(context)
    try:
        fixture_result, variable_result = await asyncio.to_thread(auto_sync_worker, settings)
    except Exception:  # noqa: BLE001 - background job must keep the bot alive.
        LOGGER.exception("Auto sync failed")
        return
    LOGGER.info(
        "Auto sync done fixtures=%s variables_contexts=%s variables_errors=%s",
        getattr(fixture_result, "imported", None),
        variable_result.contexts_upserted,
        len(variable_result.errors),
    )


def configure_auto_sync(application: Application, settings: BotSettings) -> None:
    if not settings.auto_sync_enabled:
        return
    if application.job_queue is None:
        LOGGER.warning("Auto sync requested, but JobQueue is not available")
        return
    interval_hours = max(1, settings.auto_sync_interval_hours)
    first_delay_minutes = max(1, settings.auto_sync_first_delay_minutes)
    application.job_queue.run_repeating(
        auto_sync_job,
        interval=timedelta(hours=interval_hours),
        first=timedelta(minutes=first_delay_minutes),
        name="brucebet-auto-sync",
    )
    LOGGER.info("Auto sync scheduled every %s hours after %s minute first delay", interval_hours, first_delay_minutes)


def build_application(settings: BotSettings) -> Application:
    application = ApplicationBuilder().token(settings.token).build()
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("load", load_cmd))
    application.add_handler(CommandHandler("hq", hq_cmd))
    application.add_handler(CommandHandler("table", table_cmd))
    application.add_handler(CommandHandler("field", field_cmd))
    application.add_handler(CommandHandler("recommend", recommend_cmd))
    application.add_handler(CommandHandler("odds", odds_cmd))
    application.add_handler(CommandHandler("quota", quota_cmd))
    application.add_handler(CommandHandler("sources", sources_cmd))
    application.add_handler(CommandHandler("sync_fixtures", sync_fixtures_cmd))
    application.add_handler(CommandHandler("sync_odds", sync_odds_cmd))
    application.add_handler(CommandHandler("sync_variables", sync_variables_cmd))
    application.add_handler(CommandHandler("risk", risk_cmd))
    application.add_handler(CommandHandler("strategy", strategy_cmd))
    application.add_handler(CommandHandler("calendar", calendar_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("week", week_cmd))
    application.add_handler(CommandHandler("next", next_cmd))
    application.add_handler(CommandHandler("round", round_cmd))
    application.add_handler(CommandHandler("variables", variables_cmd))
    application.add_handler(CommandHandler("match", match_cmd))
    application.add_handler(CommandHandler("dossier", dossier_cmd))
    application.add_handler(CommandHandler("vs", vs_cmd))
    application.add_handler(CommandHandler("audit", audit_cmd))
    application.add_handler(CommandHandler("deadline", deadlines_cmd))
    application.add_handler(CommandHandler("deadlines", deadlines_cmd))
    application.add_handler(CommandHandler("schedule", schedule_cmd))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    configure_auto_sync(application, settings)
    return application


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting BruceBet Telegram bot db=%s data_dir=%s", settings.db_path, settings.data_dir)
    build_application(settings).run_polling(close_loop=False)


if __name__ == "__main__":
    main()
