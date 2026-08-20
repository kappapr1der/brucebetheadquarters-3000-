from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import re
import sqlite3

from .scoring import normalize_score
from .storage import (
    active_participant_id,
    active_season_id,
    ensure_participant,
    ingest_prediction_revision,
)


SCORE_TOKEN_RE = re.compile(r"(?<!\d)(\d+\s*[:;\-–—]\s*\d+)(?!\d)")
LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*")
UNPAID_MARKER_RE = re.compile(r"\b(?:без\s+(?:взноса|оплаты)|не\s+вносит|no\s+fee)\b", re.IGNORECASE)
PAID_MARKER_RE = re.compile(r"(?:\bвзнос\b|\bоплат\w*\b|\b300\s*(?:р(?:уб)?\.?|₽)?\b|\+$)", re.IGNORECASE)


@dataclass(frozen=True)
class ExpectedMatch:
    position: int
    home: str
    away: str

    @property
    def label(self) -> str:
        return f"{self.home} - {self.away}"


@dataclass(frozen=True)
class ParsedForecast:
    position: int
    score: str
    raw_score: str
    line_number: int
    raw_line: str


@dataclass(frozen=True)
class InvalidForecast:
    position: int
    raw_score: str
    line_number: int
    raw_line: str


@dataclass(frozen=True)
class ForecastImportReport:
    matches: tuple[ExpectedMatch, ...]
    forecasts: tuple[ParsedForecast, ...]
    normalized: tuple[ParsedForecast, ...]
    invalid_lines: tuple[str, ...]
    duplicate_positions: tuple[int, ...]
    extra_lines: tuple[str, ...]
    missing_positions: tuple[int, ...]
    invalid_forecasts: tuple[InvalidForecast, ...] = ()
    stored_positions: tuple[int, ...] = ()
    protected_positions: tuple[int, ...] = ()
    rejected_positions: tuple[int, ...] = ()
    quarantined_positions: tuple[int, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.matches)

    @property
    def accepted_count(self) -> int:
        return len(self.forecasts)

    @property
    def stored_count(self) -> int:
        return len(self.stored_positions)


@dataclass(frozen=True)
class ParticipantEntry:
    name: str
    paid: bool | None


@dataclass(frozen=True)
class ParticipantImportReport:
    entries: tuple[ParticipantEntry, ...]
    duplicate_names: tuple[str, ...]

    @property
    def accepted_count(self) -> int:
        return len(self.entries)

    @property
    def paid_count(self) -> int:
        return sum(entry.paid is True for entry in self.entries)

    @property
    def unpaid_count(self) -> int:
        return sum(entry.paid is False for entry in self.entries)

    @property
    def unspecified_count(self) -> int:
        return sum(entry.paid is None for entry in self.entries)


def expected_matches(conn: sqlite3.Connection, round_name: str) -> list[ExpectedMatch]:
    rows = list(
        conn.execute(
            """
            SELECT m.position, m.home, m.away
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND r.name = ?
            ORDER BY m.position
            """,
            (active_season_id(conn), round_name.strip()),
        )
    )
    if not rows:
        raise ValueError(f"No matches found for round {round_name!r}")
    return [ExpectedMatch(int(row["position"]), row["home"], row["away"]) for row in rows]


def _clean_line(value: str) -> str:
    return LIST_PREFIX_RE.sub("", value.strip()).casefold()


def parse_participant_block(text: str) -> ParticipantImportReport:
    """Parse an operator-maintained list without guessing payment status."""
    entries: list[ParticipantEntry] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = LIST_PREFIX_RE.sub("", raw_line.strip())
        if not line:
            continue
        unpaid = UNPAID_MARKER_RE.search(line)
        paid = PAID_MARKER_RE.search(line) if unpaid is None else None
        marker = unpaid or paid
        name = line[: marker.start()].rstrip(" .,;:-—–") if marker else line.rstrip(" .")
        name = name.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            duplicates.append(name)
            continue
        seen.add(key)
        entries.append(ParticipantEntry(name, False if unpaid else True if paid else None))
    return ParticipantImportReport(tuple(entries), tuple(duplicates))


def _labelled_match(line: str, matches: list[ExpectedMatch]) -> ExpectedMatch | None:
    cleaned = _clean_line(line)
    for match in matches:
        if cleaned.startswith(match.label.casefold()):
            return match
    return None


def parse_forecast_block(text: str, matches: list[ExpectedMatch]) -> ForecastImportReport:
    """Parse one participant's forecasts in fixture order or with match labels.

    A score is accepted only when a line has exactly one one-digit score token.
    That lets us accept human punctuation variations without guessing at comments,
    duplicated scores, or an extra line after the round is complete.
    """
    parsed: list[ParsedForecast] = []
    normalized: list[ParsedForecast] = []
    invalid_lines: list[str] = []
    invalid_forecasts: list[InvalidForecast] = []
    duplicate_positions: list[int] = []
    extra_lines: list[str] = []
    assigned: set[int] = set()

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        tokens = SCORE_TOKEN_RE.findall(line)
        if not tokens:
            continue
        labelled = _labelled_match(line, matches)
        candidate_position = labelled.position if labelled is not None else next(
            (match.position for match in matches if match.position not in assigned),
            None,
        )
        if len(tokens) != 1:
            invalid_lines.append(f"line {line_number}: more than one score ({line})")
            if labelled is not None:
                invalid_forecasts.append(InvalidForecast(labelled.position, line, line_number, line))
            continue

        raw_score = tokens[0]
        score = normalize_score(raw_score)
        if score is None:
            invalid_lines.append(f"line {line_number}: unsupported score {raw_score}")
            if candidate_position is not None:
                invalid_forecasts.append(InvalidForecast(candidate_position, raw_score, line_number, line))
            continue

        if labelled is not None:
            position = labelled.position
        else:
            position = candidate_position
            if position is None:
                extra_lines.append(f"line {line_number}: {line}")
                continue

        if position in assigned:
            duplicate_positions.append(position)
            continue

        item = ParsedForecast(position, score, raw_score, line_number, line)
        parsed.append(item)
        assigned.add(position)
        if raw_score.strip() != score:
            normalized.append(item)

    expected = {match.position for match in matches}
    return ForecastImportReport(
        matches=tuple(matches),
        forecasts=tuple(sorted(parsed, key=lambda item: item.position)),
        normalized=tuple(normalized),
        invalid_lines=tuple(invalid_lines),
        duplicate_positions=tuple(sorted(set(duplicate_positions))),
        extra_lines=tuple(extra_lines),
        missing_positions=tuple(sorted(expected - assigned)),
        invalid_forecasts=tuple(invalid_forecasts),
    )


def import_forecast_block(
    conn: sqlite3.Connection,
    participant: str,
    round_name: str,
    text: str,
    submitted_at: datetime | str | None = None,
    source: str = "direct-import",
    lock_minutes: int = 90,
    source_item_id: str | None = None,
    observed_at: datetime | str | None = None,
    actor: str | None = None,
) -> ForecastImportReport:
    matches = expected_matches(conn, round_name)
    report = parse_forecast_block(text, matches)
    timestamp = submitted_at.isoformat() if isinstance(submitted_at, datetime) else submitted_at
    participant_name = participant.strip()
    if active_participant_id(conn, participant_name) is None:
        raise ValueError(
            f"Участник {participant_name!r} не зарегистрирован в активном сезоне. "
            "Сначала добавь его через список участников или дождись заявки из VK."
        )
    stored_positions: list[int] = []
    protected_positions: list[int] = []
    rejected_positions: list[int] = []
    quarantined_positions: list[int] = []
    source_item_base = source_item_id or f"{source}:{participant_name.casefold()}:{round_name.strip()}:{timestamp or 'missing'}"
    for forecast in report.forecasts:
        result = ingest_prediction_revision(
            conn,
            participant=participant_name,
            round_name=round_name,
            position=forecast.position,
            score=forecast.raw_score,
            submitted_at=submitted_at,
            source=f"{source}:line-{forecast.line_number}",
            stable_source_item_id=f"{source_item_base}:position-{forecast.position}",
            observed_at=observed_at,
            actor=actor,
            lock_minutes=lock_minutes,
        )
        if result.accepted:
            stored_positions.append(forecast.position)
        elif result.reason == "late_edit":
            protected_positions.append(forecast.position)
        elif result.decision == "rejected":
            rejected_positions.append(forecast.position)
        else:
            quarantined_positions.append(forecast.position)
    for invalid in report.invalid_forecasts:
        result = ingest_prediction_revision(
            conn,
            participant=participant_name,
            round_name=round_name,
            position=invalid.position,
            score=invalid.raw_score,
            submitted_at=submitted_at,
            source=f"{source}:line-{invalid.line_number}",
            stable_source_item_id=f"{source_item_base}:position-{invalid.position}",
            observed_at=observed_at,
            actor=actor,
            lock_minutes=lock_minutes,
        )
        if result.decision == "quarantined":
            quarantined_positions.append(invalid.position)
    conn.commit()
    return replace(
        report,
        stored_positions=tuple(stored_positions),
        protected_positions=tuple(protected_positions),
        rejected_positions=tuple(rejected_positions),
        quarantined_positions=tuple(sorted(set(quarantined_positions))),
    )


def import_participant_block(conn: sqlite3.Connection, text: str) -> ParticipantImportReport:
    report = parse_participant_block(text)
    for entry in report.entries:
        existing = conn.execute("SELECT id FROM participants WHERE name = ?", (entry.name,)).fetchone()
        paid = entry.paid if entry.paid is not None else None if existing else 0
        ensure_participant(conn, entry.name, paid=paid)
    conn.commit()
    return report
