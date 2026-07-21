from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
import sqlite3

from .scoring import normalize_score
from .storage import active_season_id, upsert_prediction


SCORE_TOKEN_RE = re.compile(r"(?<!\d)(\d+\s*[:;\-–—]\s*\d+)(?!\d)")
LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*")


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
class ForecastImportReport:
    matches: tuple[ExpectedMatch, ...]
    forecasts: tuple[ParsedForecast, ...]
    normalized: tuple[ParsedForecast, ...]
    invalid_lines: tuple[str, ...]
    duplicate_positions: tuple[int, ...]
    extra_lines: tuple[str, ...]
    missing_positions: tuple[int, ...]

    @property
    def expected_count(self) -> int:
        return len(self.matches)

    @property
    def accepted_count(self) -> int:
        return len(self.forecasts)


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
        if len(tokens) != 1:
            invalid_lines.append(f"line {line_number}: more than one score ({line})")
            continue

        raw_score = tokens[0]
        score = normalize_score(raw_score)
        if score is None:
            invalid_lines.append(f"line {line_number}: unsupported score {raw_score}")
            continue

        labelled = _labelled_match(line, matches)
        if labelled is not None:
            position = labelled.position
        else:
            position = next((match.position for match in matches if match.position not in assigned), None)
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
    )


def import_forecast_block(
    conn: sqlite3.Connection,
    participant: str,
    round_name: str,
    text: str,
    submitted_at: datetime | str | None = None,
    source: str = "direct-import",
) -> ForecastImportReport:
    matches = expected_matches(conn, round_name)
    report = parse_forecast_block(text, matches)
    timestamp = submitted_at.isoformat() if isinstance(submitted_at, datetime) else submitted_at
    for forecast in report.forecasts:
        upsert_prediction(
            conn,
            participant=participant.strip(),
            round_name=round_name.strip(),
            position=forecast.position,
            score=forecast.score,
            submitted_at=timestamp,
            source=f"{source}:line-{forecast.line_number}",
        )
    conn.commit()
    return report
