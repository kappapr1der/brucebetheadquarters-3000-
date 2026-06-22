from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ServiceReply:
    key: str
    text: str


@dataclass(frozen=True)
class DeadlineReminderPlanItem:
    key: str
    send_at: datetime
    offset_minutes: int
    reply: ServiceReply


DEFAULT_REMINDER_OFFSETS_MINUTES = (24 * 60, 6 * 60, 3 * 60, 60, 20)


def _dt(value: datetime | None) -> str:
    return value.isoformat() if value else "не указан"


def _offset_label(offset_minutes: int) -> str:
    if offset_minutes % 60 == 0:
        hours = offset_minutes // 60
        if hours == 24:
            return "24 часа"
        if hours == 6:
            return "6 часов"
        if hours == 3:
            return "3 часа"
        if hours == 1:
            return "1 час"
        return f"{hours} ч"
    return f"{offset_minutes} минут"


def participants_loaded(count: int, paid_count: int) -> list[ServiceReply]:
    bank = paid_count * 300
    return [
        ServiceReply("accepted", f"Принято. Участников: {count}, с взносом: {paid_count}."),
        ServiceReply("bank", f"Текущий банк: {bank} руб."),
        ServiceReply("next", "Теперь кидай шаблон тура или прогнозы участников."),
    ]


def round_template_loaded(round_name: str, match_count: int, deadline_at: datetime | None) -> list[ServiceReply]:
    return [
        ServiceReply("accepted", f"Принято. Тур {round_name}, матчей: {match_count}."),
        ServiceReply("deadline", f"Дедлайн тура: {_dt(deadline_at)}."),
        ServiceReply("next", "Теперь кидай свой прогноз или прогнозы участников."),
    ]


def user_forecast_loaded(
    participant: str,
    round_name: str,
    rows_count: int,
    expected_count: int,
    invalid_count: int = 0,
    nonstandard_count: int = 0,
    missing_count: int = 0,
    late_needs_check_count: int = 0,
) -> list[ServiceReply]:
    replies = [
        ServiceReply("accepted", f"Принято, {participant}. Прогноз на тур {round_name} сохранён."),
        ServiceReply("count", f"Вижу матчей: {rows_count}/{expected_count}."),
    ]
    if missing_count:
        replies.append(ServiceReply("missing", f"Не хватает прогнозов: {missing_count}. Запущу аудит."))
    if nonstandard_count:
        replies.append(
            ServiceReply(
                "nonstandard",
                f"Нестандартных форматов счёта: {nonstandard_count}. Я их принял и нормализовал.",
            )
        )
    if invalid_count:
        replies.append(ServiceReply("invalid", f"Нечитаемых счетов: {invalid_count}. Их надо проверить вручную."))
    if late_needs_check_count:
        replies.append(
            ServiceReply(
                "late_check",
                f"После дедлайна: {late_needs_check_count}. Нужны kickoff-времена, чтобы понять, что засчитывается.",
            )
        )
    if not missing_count and not invalid_count and not nonstandard_count and not late_needs_check_count:
        replies.append(ServiceReply("ok", "Формат выглядит нормально. Можно ехать дальше."))
    replies.append(ServiceReply("next", "Теперь кидай прогнозы участников."))
    return replies


def participant_forecasts_loaded(
    round_name: str,
    participants_count: int,
    prediction_rows: int,
    missing_groups: int = 0,
    invalid_rows: int = 0,
    nonstandard_rows: int = 0,
) -> list[ServiceReply]:
    replies = [
        ServiceReply("accepted", f"Принято. Прогнозы участников на тур {round_name} загружены."),
        ServiceReply("count", f"Участников: {participants_count}, строк прогнозов: {prediction_rows}."),
    ]
    if missing_groups or invalid_rows or nonstandard_rows:
        if nonstandard_rows:
            replies.append(
                ServiceReply(
                    "nonstandard",
                    f"Нестандартных, но принятых счетов: {nonstandard_rows}. Я их нормализовал для анализа.",
                )
            )
        replies.append(
            ServiceReply(
                "audit_needed",
                f"Есть что проверить: неполных блоков {missing_groups}, нечитаемых счетов {invalid_rows}.",
            )
        )
        replies.append(ServiceReply("next", "Сейчас уместно вызвать /audit, потом /recommend по матчам."))
    else:
        replies.append(ServiceReply("next", "Теперь можно смотреть /field, /recommend и /vs."))
    return replies


def recommendations_ready(round_name: str, match_count: int) -> list[ServiceReply]:
    return [
        ServiceReply("ready", f"Рекомендации по туру {round_name} готовы. Матчей: {match_count}."),
        ServiceReply("next", "Можно пройтись по матчам или открыть самые рискованные отличия от поля."),
    ]


def deadline_reminder(deadline_at: datetime, hours_left: float) -> ServiceReply:
    if hours_left >= 24:
        lead = "До дедлайна примерно сутки."
    elif hours_left >= 6:
        lead = "До дедлайна около шести часов."
    elif hours_left >= 3:
        lead = "До дедлайна три часа."
    elif hours_left >= 1:
        lead = "До дедлайна около часа."
    else:
        lead = "До дедлайна меньше часа."
    return ServiceReply("deadline_reminder", f"{lead} Дедлайн: {_dt(deadline_at)}. Пора не играть в героя дедлайна.")


def deadline_reminder_for_offset(deadline_at: datetime, offset_minutes: int) -> ServiceReply:
    label = _offset_label(offset_minutes)
    if offset_minutes >= 24 * 60:
        text = (
            f"До дедлайна {label}. Дедлайн: {_dt(deadline_at)}.\n"
            "Пора собрать матчи, проверить травмы/коэффициенты и не оставлять это на последний вдох."
        )
    elif offset_minutes >= 6 * 60:
        text = (
            f"До дедлайна {label}. Дедлайн: {_dt(deadline_at)}.\n"
            "Самое время собрать поле участников и наметить базовые счета."
        )
    elif offset_minutes >= 3 * 60:
        text = (
            f"До дедлайна {label}. Дедлайн: {_dt(deadline_at)}.\n"
            "Проверь /audit, /field и спорные матчи. Тут обычно рождаются минусы, если зевнуть."
        )
    elif offset_minutes >= 60:
        text = (
            f"До дедлайна {label}. Дедлайн: {_dt(deadline_at)}.\n"
            "Пора финализировать прогноз. Уже не время строить космическую теорию всего."
        )
    else:
        text = (
            f"До дедлайна {label}. Дедлайн: {_dt(deadline_at)}.\n"
            "Последний нормальный шанс отправить прогноз и не играть в частично позднего гения."
        )
    return ServiceReply(f"deadline_minus_{offset_minutes}m", text)


def deadline_after_message(deadline_at: datetime) -> ServiceReply:
    return ServiceReply(
        "deadline_passed",
        (
            f"Дедлайн прошёл: {_dt(deadline_at)}.\n"
            "Если прогноз ещё не отправлен, можно спасать только матчи, которые не попали в окно kickoff - 90 минут."
        ),
    )


def deadline_schedule_created(
    round_name: str,
    deadline_at: datetime,
    offsets_minutes: tuple[int, ...] = DEFAULT_REMINDER_OFFSETS_MINUTES,
) -> list[ServiceReply]:
    labels = ", ".join(_offset_label(value) for value in offsets_minutes)
    return [
        ServiceReply("deadline_schedule_created", f"Напоминания на тур {round_name} поставлены."),
        ServiceReply("deadline", f"Дедлайн: {_dt(deadline_at)}."),
        ServiceReply("schedule", f"Напомню за: {labels}."),
    ]


def deadline_reminder_schedule(
    deadline_at: datetime,
    offsets_minutes: tuple[int, ...] = DEFAULT_REMINDER_OFFSETS_MINUTES,
) -> list[DeadlineReminderPlanItem]:
    return [
        DeadlineReminderPlanItem(
            key=f"deadline_minus_{offset_minutes}m",
            send_at=deadline_at - timedelta(minutes=offset_minutes),
            offset_minutes=offset_minutes,
            reply=deadline_reminder_for_offset(deadline_at, offset_minutes),
        )
        for offset_minutes in offsets_minutes
    ]


def render(replies: list[ServiceReply]) -> str:
    return "\n".join(reply.text for reply in replies)
