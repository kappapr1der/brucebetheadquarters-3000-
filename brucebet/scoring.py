from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re


STRICT_SCORE_RE = re.compile(r"^([0-9]):([0-9])$")
FLEX_SCORE_RE = re.compile(r"^([0-9])\s*[:;\-]\s*([0-9])$")


@dataclass(frozen=True)
class Score:
    home: int
    away: int

    @property
    def diff(self) -> int:
        return self.home - self.away

    @property
    def outcome(self) -> int:
        if self.home > self.away:
            return 1
        if self.home < self.away:
            return -1
        return 0

    def label(self) -> str:
        return f"{self.home}:{self.away}"


@dataclass(frozen=True)
class ScoreAward:
    points: int
    category: str


def parse_score(raw: str | None) -> Score | None:
    """Parse human-entered one-digit scores.

    The official contest template asks for ``2:0`` exactly, but people send
    ``2-0``, ``2;0`` and ``2: 0``. BruceBet accepts those for analysis while
    keeping two-digit scores invalid.
    """
    if raw is None:
        return None
    match = FLEX_SCORE_RE.fullmatch(raw.strip())
    if not match:
        return None
    return Score(int(match.group(1)), int(match.group(2)))


def is_standard_score(raw: str | None) -> bool:
    if raw is None:
        return False
    return STRICT_SCORE_RE.fullmatch(raw.strip()) is not None


def normalize_score(raw: str | None) -> str | None:
    score = parse_score(raw)
    return score.label() if score else None


def score_prediction(prediction: Score | None, result: Score | None) -> ScoreAward:
    if prediction is None:
        return ScoreAward(0, "invalid")
    if result is None:
        return ScoreAward(0, "pending")
    if prediction == result:
        return ScoreAward(3, "exact")
    if prediction.diff == result.diff:
        return ScoreAward(2, "diff")
    if prediction.outcome == result.outcome:
        return ScoreAward(1, "outcome")
    return ScoreAward(0, "miss")


def parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def is_prediction_eligible(
    submitted_at: datetime | None,
    kickoff_at: datetime | None,
    lock_minutes: int = 90,
) -> bool:
    if submitted_at is None or kickoff_at is None:
        return True
    return submitted_at <= kickoff_at - timedelta(minutes=lock_minutes)
