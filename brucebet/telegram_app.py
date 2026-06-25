from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
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
    compare_participants,
    compute_standings,
    field_summary,
    hq_summary,
    match_header,
    prediction_views_for_match,
    recommend_match,
    risk_map,
    round_deadlines,
    strategy_summary,
)
from .scoring import is_standard_score, normalize_score, parse_datetime, parse_score
from .service_messages import (
    deadline_after_message,
    deadline_reminder_schedule,
    deadline_schedule_created,
    participant_forecasts_loaded,
    render,
)
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


def parse_chat_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


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


def clean(value: object) -> str:
    return "" if value is None else str(value)


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
        "Команды: /hq, /load, /table, /field, /recommend, /risk, /strategy, /match, /vs, /deadlines, /schedule, /audit.",
    ]
    if not settings.allowed_chat_ids:
        lines.append("")
        lines.append("Внимание: TELEGRAM_ALLOWED_CHAT_IDS не задан, бот открыт для любого чата с токеном.")
    await send_text(update, "\n".join(lines))


@require_access
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_text(
        update,
        "\n".join(
            [
                "/load - пришли VK-пасту текстом или файлом",
                "/hq - штаб активного тура",
                "/table - таблица конкурса",
                "/field <матч> - поле прогнозов",
                "/recommend <матч> - рекомендация по матчу",
                "/risk [тур] - риск-карта тура",
                "/strategy - стратегия относительно таблицы",
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


def build_application(settings: BotSettings) -> Application:
    application = ApplicationBuilder().token(settings.token).build()
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("load", load_cmd))
    application.add_handler(CommandHandler("hq", hq_cmd))
    application.add_handler(CommandHandler("table", table_cmd))
    application.add_handler(CommandHandler("field", field_cmd))
    application.add_handler(CommandHandler("recommend", recommend_cmd))
    application.add_handler(CommandHandler("risk", risk_cmd))
    application.add_handler(CommandHandler("strategy", strategy_cmd))
    application.add_handler(CommandHandler("match", match_cmd))
    application.add_handler(CommandHandler("vs", vs_cmd))
    application.add_handler(CommandHandler("audit", audit_cmd))
    application.add_handler(CommandHandler("deadline", deadlines_cmd))
    application.add_handler(CommandHandler("deadlines", deadlines_cmd))
    application.add_handler(CommandHandler("schedule", schedule_cmd))
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return application


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting BruceBet Telegram bot db=%s data_dir=%s", settings.db_path, settings.data_dir)
    build_application(settings).run_polling(close_loop=False)


if __name__ == "__main__":
    main()
