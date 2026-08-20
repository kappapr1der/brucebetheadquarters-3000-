from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
import re
import sys
from typing import Literal

from .scoring import normalize_score
from .vk_board import (
    DEFAULT_BROWSER_WAIT_MS,
    DEFAULT_CHROMIUM_BIN,
    VkBrowserError,
    VkPublicTopicResult,
    parse_topic_url,
    probe_public_topic,
)
from .vk_parser import MSK, MONTHS, MatchTemplate, RoundTemplate, parse_author, parse_ru_datetime, parse_templates


TopicKind = Literal["registration", "predictions"]
LeagueHint = Literal["epl", "non_epl", "unknown"]
RegistrationState = Literal["open", "closed", "unknown"]
FeeIntent = Literal["paid_declared", "free", "unknown"]

DATE_ONLY_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[а-яё]{3})\s+(?P<year>\d{4})\s+в\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
EN_DATE_ONLY_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(?P<year>\d{4})\s+at\s+(?P<time>\d{1,2}:\d{2})(?:\s*(?P<meridiem>am|pm))?$",
    re.IGNORECASE,
)
RELATIVE_DATE_RE = re.compile(
    r"^(?P<day>today|yesterday|сегодня|вчера)\s+(?:at|в)\s+(?P<time>\d{1,2}:\d{2})(?:\s*(?P<meridiem>am|pm))?$",
    re.IGNORECASE,
)
SCORE_TAIL_RE = re.compile(r"(?P<score>\d+\s*[:;\-]\s*\d+)\s*$")
IDENTITY_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .'-]{0,79}$")
PARENTHETICAL_IDENTITY_RE = re.compile(r"\((?P<name>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .'-]{0,79})\)")
FREE_FEE_RE = re.compile(
    r"\b(?:без\s+(?:взнос\w*|оплат\w*)|не\s+вношу|без\s+участия\s+в\s+приз)\b",
    re.IGNORECASE,
)
PAID_FEE_RE = re.compile(
    r"\b(?:взнос|взн\w*|вн[её]с|оплатил|отправил|перев[её]л|закинул|[1-9]\d{2,3}\s*(?:р(?:уб)?\.?|₽)?)\b",
    re.IGNORECASE,
)
REGISTRATION_MARKER_SUFFIX_RE = re.compile(
    r"\s*[,.;:—–-]?\s*(?:без\s+(?:взнос\w*|оплат\w*)|не\s+вношу|взнос\w*\s*\d{2,4}|взн\w*\s*\d{2,4}|\d{2,4}\s*(?:р(?:уб)?\.?|₽)?)\s*$",
    re.IGNORECASE,
)
CLOSED_RE = re.compile(
    r"\b(?:регистрац\w*\s+закрыт\w*|при[её]м\s+участник\w*\s+закрыт\w*|состав\s+сформирован\w*)\b",
    re.IGNORECASE,
)
OPEN_RE = re.compile(r"\b(?:регистрац\w*|для\s+участия|записыва(?:ем|йтесь))\b", re.IGNORECASE)
FINAL_ROSTER_RE = re.compile(r"\b(?:финальн\w*\s+(?:состав|список)|итогов\w*\s+состав)\b", re.IGNORECASE)
NON_EPL_RE = re.compile(r"\b(?:рпл|российск\w*\s+премьер)\b", re.IGNORECASE)
EPL_RE = re.compile(r"\b(?:апл|английск\w*\s+премьер|premier\s+league)\b", re.IGNORECASE)
EN_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
NOISE = {
    "",
    "показать список оценивших",
    "показать реакции",
    "ответить",
    "поделиться",
    "show likes",
    "show reactions",
    "show more posts",
    "загружается",
    "go up",
    "read only the most interesting posts",
    "we'll find posts according to your preferences and create a whole feed from them. just sign in to check it out",
    "sign up",
    "sign in",
    "reply",
    "share",
}


@dataclass(frozen=True)
class VkComment:
    source_key: str
    author: str
    submitted_at: datetime
    source_line: int
    body_lines: tuple[str, ...]


@dataclass(frozen=True)
class VkRecognizedForecast:
    position: int
    match_label: str
    raw_score: str
    normalized_score: str
    source_line: int


@dataclass(frozen=True)
class VkForecastSubmission:
    source_key: str
    vk_author: str
    participant: str
    submitted_at: datetime
    round_name: str
    deadline_at: datetime
    expected_matches: int
    forecasts: tuple[VkRecognizedForecast, ...]
    status: Literal["full", "partial"]
    warnings: tuple[str, ...]

    @property
    def is_full(self) -> bool:
        return self.status == "full"


@dataclass(frozen=True)
class VkRegistrationEntry:
    source_key: str
    vk_author: str
    participant: str
    submitted_at: datetime
    fee_intent: FeeIntent
    fee_amount_rub: int | None
    payment_status: Literal["confirmed", "not_applicable", "unknown"]
    source_line: int


@dataclass(frozen=True)
class VkTopicDryRunReport:
    topic_kind: TopicKind
    group_id: int
    topic_id: int
    url: str
    captured_at: datetime
    content_fingerprint: str
    title: str
    league_hint: LeagueHint
    comments: tuple[VkComment, ...]
    templates: tuple[RoundTemplate, ...]
    forecast_submissions: tuple[VkForecastSubmission, ...]
    registration_entries: tuple[VkRegistrationEntry, ...]
    registration_state: RegistrationState
    final_roster_detected: bool
    warnings: tuple[str, ...]

    @property
    def future_ingestion_allowed(self) -> bool:
        return self.league_hint == "epl"


@dataclass(frozen=True)
class VkPublicTopicCapture:
    """One read-only public VK fetch plus its structured interpretation."""

    report: VkTopicDryRunReport
    visible_text: str
    html_chars: int
    visible_chars: int
    score_line_count: int


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not _is_noise_line(line)]


def _is_noise_line(value: str) -> bool:
    compact = " ".join(value.casefold().split()).rstrip(".…")
    return compact in NOISE


def _date_only(line: str) -> datetime | None:
    match = DATE_ONLY_RE.match(line.strip())
    if match:
        month = match.group("month").lower()
        if month in MONTHS:
            return parse_ru_datetime(match.group("day"), month, match.group("year"), match.group("time"))

    english = EN_DATE_ONLY_RE.match(line.strip())
    if english is not None:
        hour, minute = (int(value) for value in english.group("time").split(":"))
        meridiem = (english.group("meridiem") or "").lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        month = EN_MONTHS[english.group("month")[:3].lower()]
        return datetime(int(english.group("year")), month, int(english.group("day")), hour, minute, tzinfo=MSK)

    relative = RELATIVE_DATE_RE.match(line.strip())
    if relative is None:
        return None
    hour, minute = (int(value) for value in relative.group("time").split(":"))
    meridiem = (relative.group("meridiem") or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    now = datetime.now(MSK)
    date_value = now.date() - timedelta(days=1 if relative.group("day").casefold() in {"yesterday", "вчера"} else 0)
    return datetime(date_value.year, date_value.month, date_value.day, hour, minute, tzinfo=MSK)


def _comment_key(group_id: int, topic_id: int, author: str, submitted_at: datetime) -> str:
    author_key = re.sub(r"\s+", "-", author.casefold()).strip("-") or "unknown"
    return f"vk:{group_id}:{topic_id}:{submitted_at.isoformat()}:{author_key}"


def parse_comment_blocks(text: str, *, group_id: int, topic_id: int) -> list[VkComment]:
    """Split Chromium visible text into comments without relying on VK API data.

    VK sometimes renders the author and timestamp as two neighbouring text nodes.
    Both the combined export form and that split-browser form are accepted here.
    """

    lines = _clean_lines(text)
    headers: list[tuple[str, datetime, int, int]] = []
    for index, line in enumerate(lines):
        direct = parse_author(line)
        if direct is not None:
            headers.append((direct[0], direct[1], index, index + 1))
            continue

        submitted_at = _date_only(line)
        if submitted_at is None or index == 0:
            continue
        author = lines[index - 1].strip()
        if not author or _is_noise_line(author) or parse_author(author) is not None:
            continue
        headers.append((author, submitted_at, index - 1, index + 1))

    headers.sort(key=lambda item: item[2])
    comments: list[VkComment] = []
    for ordinal, (author, submitted_at, source_start, body_start) in enumerate(headers, start=1):
        next_start = headers[ordinal][2] if ordinal < len(headers) else len(lines)
        body = tuple(lines[body_start:next_start])
        comments.append(
            VkComment(
                source_key=_comment_key(group_id, topic_id, author, submitted_at),
                author=author,
                submitted_at=submitted_at,
                source_line=source_start + 1,
                body_lines=body,
            )
        )
    return comments


def _canonical_label(value: str) -> str:
    value = value.replace("–", "-").replace("—", "-")
    return " ".join(value.casefold().split())


def _score_for_match_line(line: str, match: MatchTemplate) -> str | None:
    score_match = SCORE_TAIL_RE.search(line)
    if score_match is None:
        return None
    label = line[: score_match.start()].strip()
    if _canonical_label(label) != _canonical_label(match.label):
        return None
    return score_match.group("score")


def _looks_like_identity(line: str) -> bool:
    compact = " ".join(line.strip().split())
    if _is_noise_line(compact) or not IDENTITY_RE.fullmatch(compact):
        return False
    lowered = compact.casefold()
    return not any(word in lowered for word in ("взнос", "руб", "оплат", "отправ", "прогноз", "регистрац"))


def _participant_for_comment(comment: VkComment) -> str:
    for line in comment.body_lines:
        if SCORE_TAIL_RE.search(line) or " - " in line:
            continue
        parenthetical = PARENTHETICAL_IDENTITY_RE.search(line)
        if parenthetical is not None and _looks_like_identity(parenthetical.group("name")):
            return " ".join(parenthetical.group("name").split())
        without_marker = REGISTRATION_MARKER_SUFFIX_RE.sub("", line).strip(" .,;:-—–")
        if without_marker != line and _looks_like_identity(without_marker):
            return without_marker
        if _looks_like_identity(line):
            return line.rstrip(".")
    return comment.author


def _forecast_submission(comment: VkComment, template: RoundTemplate) -> VkForecastSubmission | None:
    by_position: dict[int, VkRecognizedForecast] = {}
    warnings: list[str] = []
    template_matches = {match.position: match for match in template.matches}

    for line_offset, line in enumerate(comment.body_lines):
        matches = [(match, _score_for_match_line(line, match)) for match in template.matches]
        matches = [(match, raw_score) for match, raw_score in matches if raw_score is not None]
        if not matches:
            continue
        match, raw_score = matches[0]
        normalized_score = normalize_score(raw_score)
        if normalized_score is None:
            warnings.append(f"{match.label}: unsupported score '{raw_score}'")
            continue
        candidate = VkRecognizedForecast(
            position=match.position,
            match_label=match.label,
            raw_score=raw_score,
            normalized_score=normalized_score,
            source_line=comment.source_line + line_offset + 1,
        )
        previous = by_position.get(match.position)
        if previous is not None:
            warnings.append(
                f"{match.label}: duplicate scores {previous.normalized_score} and {candidate.normalized_score}"
            )
            continue
        by_position[match.position] = candidate

    if not by_position:
        return None

    forecasts = tuple(by_position[position] for position in sorted(by_position))
    expected_matches = len(template_matches)
    status: Literal["full", "partial"] = "full" if len(forecasts) == expected_matches else "partial"
    if status == "partial":
        missing = [str(position) for position in template_matches if position not in by_position]
        warnings.append(f"missing fixture positions: {', '.join(missing)}")

    return VkForecastSubmission(
        source_key=comment.source_key,
        vk_author=comment.author,
        participant=_participant_for_comment(comment),
        submitted_at=comment.submitted_at,
        round_name=template.round_name,
        deadline_at=template.deadline_at,
        expected_matches=expected_matches,
        forecasts=forecasts,
        status=status,
        warnings=tuple(warnings),
    )


def _fee_intent(comment: VkComment) -> FeeIntent:
    text = "\n".join(comment.body_lines)
    if FREE_FEE_RE.search(text):
        return "free"
    if PAID_FEE_RE.search(text):
        return "paid_declared"
    return "unknown"


def _fee_amount_rub(comment: VkComment, fee_intent: FeeIntent) -> int | None:
    """Return the declared entry fee without treating it as confirmed payment."""

    if fee_intent != "paid_declared":
        return None
    for line in comment.body_lines:
        match = re.search(r"(?<!\d)([1-9]\d{2,3})(?!\d)", line)
        if match is not None:
            return int(match.group(1))
    return None


def _league_hint(text: str) -> LeagueHint:
    if NON_EPL_RE.search(text):
        return "non_epl"
    if EPL_RE.search(text):
        return "epl"
    return "unknown"


def _registration_state(text: str) -> RegistrationState:
    if CLOSED_RE.search(text):
        return "closed"
    if OPEN_RE.search(text):
        return "open"
    return "unknown"


def _title(lines: list[str]) -> str:
    for line in lines[:30]:
        if parse_author(line) is not None or _date_only(line) is not None:
            continue
        lowered = line.casefold()
        if any(token in lowered for token in ("прогноз", "регистрац", "участ", "апл", "рпл")):
            return line
    return lines[0] if lines else ""


def _parse_topic(
    *,
    group_id: int,
    topic_id: int,
    url: str,
    title: str,
    source_text: str,
    topic_kind: TopicKind,
    comments: tuple[VkComment, ...],
) -> VkTopicDryRunReport:
    lines = _clean_lines(source_text)
    templates = tuple(parse_templates(lines))
    warnings: list[str] = []
    forecast_submissions: list[VkForecastSubmission] = []
    registration_entries: list[VkRegistrationEntry] = []

    if topic_kind == "predictions":
        if not templates:
            warnings.append("no round template was recognized")
        for comment in comments:
            for template in templates:
                submission = _forecast_submission(comment, template)
                if submission is not None:
                    forecast_submissions.append(submission)
        if not forecast_submissions and templates:
            warnings.append("no participant forecast blocks were recognized")
    else:
        for comment in comments:
            if comment.author.casefold() == "forecasters club":
                continue
            fee_intent = _fee_intent(comment)
            registration_entries.append(
                VkRegistrationEntry(
                    source_key=comment.source_key,
                    vk_author=comment.author,
                    participant=_participant_for_comment(comment),
                    submitted_at=comment.submitted_at,
                    fee_intent=fee_intent,
                    fee_amount_rub=_fee_amount_rub(comment, fee_intent),
                    payment_status=(
                        "confirmed"
                        if fee_intent == "paid_declared"
                        else "not_applicable"
                        if fee_intent == "free"
                        else "unknown"
                    ),
                    source_line=comment.source_line,
                )
            )
        if not registration_entries:
            warnings.append("no registration comments were recognized")

    league_hint = _league_hint(source_text)
    if league_hint != "epl":
        warnings.append("non-EPL or unknown topic: dry-run is allowed, future ingestion must stay disabled")

    return VkTopicDryRunReport(
        topic_kind=topic_kind,
        group_id=group_id,
        topic_id=topic_id,
        url=url,
        captured_at=datetime.now(timezone.utc),
        content_fingerprint=sha256(source_text.encode("utf-8")).hexdigest(),
        title=title or _title(lines),
        league_hint=league_hint,
        comments=comments,
        templates=templates,
        forecast_submissions=tuple(forecast_submissions),
        registration_entries=tuple(registration_entries),
        registration_state=_registration_state(source_text),
        final_roster_detected=bool(FINAL_ROSTER_RE.search(source_text)),
        warnings=tuple(warnings),
    )


def parse_public_topic_result(result: VkPublicTopicResult, topic_kind: TopicKind) -> VkTopicDryRunReport:
    """Build a structured report from a public topic without opening SQLite."""

    return _parse_topic(
        group_id=result.group_id,
        topic_id=result.topic_id,
        url=result.url,
        title=_title(_clean_lines(result.text)),
        source_text=result.text,
        topic_kind=topic_kind,
        comments=tuple(parse_comment_blocks(result.text, group_id=result.group_id, topic_id=result.topic_id)),
    )


def parse_api_topic_result(
    *,
    group_id: int,
    topic_id: int,
    url: str,
    title: str,
    topic_kind: TopicKind,
    comments: tuple[VkComment, ...],
) -> VkTopicDryRunReport:
    """Parse VK API comments with the same rules as public Chromium output.

    The comment objects retain their immutable VK comment IDs as source keys,
    while the shared parser still owns template, score, fee and league logic.
    """

    source_lines = [title]
    for comment in comments:
        source_lines.extend(comment.body_lines)
    return _parse_topic(
        group_id=group_id,
        topic_id=topic_id,
        url=url,
        title=title,
        source_text="\n".join(line for line in source_lines if line),
        topic_kind=topic_kind,
        comments=comments,
    )


def read_public_topic_dry_run(
    topic_url: str,
    topic_kind: TopicKind,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    wait_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
) -> VkTopicDryRunReport:
    return capture_public_topic_dry_run(
        topic_url,
        topic_kind,
        chromium_bin=chromium_bin,
        wait_ms=wait_ms,
        timeout=timeout,
    ).report


def capture_public_topic_dry_run(
    topic_url: str,
    topic_kind: TopicKind,
    *,
    chromium_bin: str = DEFAULT_CHROMIUM_BIN,
    wait_ms: int = DEFAULT_BROWSER_WAIT_MS,
    timeout: int = 45,
) -> VkPublicTopicCapture:
    """Read a public topic once and retain the exact visible source text."""

    group_id, topic_id = parse_topic_url(topic_url)
    result = probe_public_topic(
        group_id,
        topic_id,
        chromium_bin=chromium_bin,
        virtual_time_ms=wait_ms,
        timeout=timeout,
    )
    return VkPublicTopicCapture(
        report=parse_public_topic_result(result, topic_kind),
        visible_text=result.text,
        html_chars=result.html_chars,
        visible_chars=result.visible_chars,
        score_line_count=result.score_line_count,
    )


def render_dry_run_report(report: VkTopicDryRunReport, *, limit: int = 60) -> str:
    lines = [
        "VK dry-run only: SQLite and VK write actions are disabled.",
        f"topic: {report.url}",
        f"kind: {report.topic_kind}",
        f"title: {report.title or '(not recognized)'}",
        f"league gate: {report.league_hint}",
        f"captured_at: {report.captured_at.isoformat()}",
        f"content_fingerprint: {report.content_fingerprint}",
        f"comments: {len(report.comments)}",
    ]

    if report.topic_kind == "predictions":
        lines.append(f"templates: {len(report.templates)}")
        for template in report.templates:
            lines.append(
                f"  round {template.round_name}: matches={len(template.matches)}, deadline={template.deadline_at.isoformat()}"
            )
        lines.append(f"forecast blocks: {len(report.forecast_submissions)}")
        for item in report.forecast_submissions[: max(0, limit)]:
            lines.append(
                f"  {item.status.upper()} {item.participant} (VK: {item.vk_author}) "
                f"round={item.round_name} {len(item.forecasts)}/{item.expected_matches} at {item.submitted_at.isoformat()}"
            )
            if item.warnings:
                lines.append(f"    warnings: {' | '.join(item.warnings)}")
    else:
        lines.extend(
            [
                f"registration state: {report.registration_state}",
                f"final roster marker: {'yes' if report.final_roster_detected else 'no'}",
                f"registration entries: {len(report.registration_entries)}",
            ]
        )
        for item in report.registration_entries[: max(0, limit)]:
            lines.append(
                f"  {item.participant} (VK: {item.vk_author}) fee={item.fee_intent} "
                f"amount={item.fee_amount_rub or '-'} payment={item.payment_status} at {item.submitted_at.isoformat()}"
            )

    if report.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in report.warnings)
    return "\n".join(lines)


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run parse a public Forecasters Club VK topic. No SQLite writes.")
    parser.add_argument("--topic-url", required=True, help="Public VK discussion URL.")
    parser.add_argument("--kind", choices=("registration", "predictions"), required=True)
    parser.add_argument("--chromium", default=os.getenv("VK_CHROMIUM_BIN", DEFAULT_CHROMIUM_BIN).strip())
    parser.add_argument("--wait-ms", type=int, default=_env_int("VK_BROWSER_WAIT_MS", DEFAULT_BROWSER_WAIT_MS))
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--limit", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = read_public_topic_dry_run(
            args.topic_url,
            args.kind,
            chromium_bin=args.chromium,
            wait_ms=args.wait_ms,
            timeout=args.timeout,
        )
    except (ValueError, VkBrowserError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(render_dry_run_report(report, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

