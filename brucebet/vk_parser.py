from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re


MSK = timezone(timedelta(hours=3))
MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

TEMPLATE_RE = re.compile(
    r"^Шаблон .*?,\s*(?P<round>\d+)-й тур\.\s*Дедлайн\s*"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}),\s*(?P<time>\d{1,2}:\d{2})$"
)
AUTHOR_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<day>\d{1,2})\s+(?P<month>[а-яё]{3})\s+"
    r"(?P<year>\d{4})\s+в\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
NOISE = {"", "Показать список оценивших", "Ответить"}
SCORE_RE = re.compile(r"(?P<score>\d+\s*[:;\-]\s*\d+)$")


@dataclass(frozen=True)
class MatchTemplate:
    round_name: str
    position: int
    home: str
    away: str

    @property
    def label(self) -> str:
        return f"{self.home} - {self.away}"


@dataclass(frozen=True)
class RoundTemplate:
    round_name: str
    deadline_at: datetime
    matches: list[MatchTemplate]


@dataclass(frozen=True)
class PredictionRecord:
    participant: str
    round_name: str
    position: int
    score: str
    submitted_at: datetime
    source: str


def parse_ru_datetime(day: str, month: str, year: str, time_value: str) -> datetime:
    hour, minute = [int(part) for part in time_value.split(":")]
    return datetime(int(year), MONTHS[month.lower()], int(day), hour, minute, tzinfo=MSK)


def parse_deadline(date_value: str, time_value: str) -> datetime:
    day, month, year = [int(part) for part in date_value.split(".")]
    hour, minute = [int(part) for part in time_value.split(":")]
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


def is_match_only(line: str) -> bool:
    if " - " not in line:
        return False
    return SCORE_RE.search(line) is None


def split_match(line: str) -> tuple[str, str]:
    home, away = line.split(" - ", 1)
    return home.strip(), away.strip()


def parse_templates(lines: list[str]) -> list[RoundTemplate]:
    templates: dict[str, RoundTemplate] = {}
    for index, raw in enumerate(lines):
        line = raw.strip()
        match = TEMPLATE_RE.match(line)
        if not match:
            continue

        round_name = match.group("round")
        deadline_at = parse_deadline(match.group("date"), match.group("time"))
        parsed_matches: list[MatchTemplate] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if not candidate:
                cursor += 1
                continue
            if not is_match_only(candidate):
                break
            home, away = split_match(candidate)
            parsed_matches.append(
                MatchTemplate(
                    round_name=round_name,
                    position=len(parsed_matches) + 1,
                    home=home,
                    away=away,
                )
            )
            cursor += 1

        if len(parsed_matches) >= 10 and round_name not in templates:
            templates[round_name] = RoundTemplate(round_name, deadline_at, parsed_matches)

    return [templates[key] for key in sorted(templates.keys(), key=int)]


def parse_author(line: str) -> tuple[str, datetime] | None:
    match = AUTHOR_RE.match(line.strip())
    if not match:
        return None
    return (
        match.group("name").strip(),
        parse_ru_datetime(
            match.group("day"),
            match.group("month"),
            match.group("year"),
            match.group("time"),
        ),
    )


def prediction_score_for_line(line: str, template: MatchTemplate) -> str | None:
    prefix = template.label + " "
    if not line.startswith(prefix):
        return None
    value = line[len(prefix) :].strip()
    return value if SCORE_RE.fullmatch(value) else None


def moderator_participant(author: str, block: list[str], templates: list[RoundTemplate]) -> str:
    if author != "Forecasters Club":
        return author
    labels = {match.label for template in templates for match in template.matches}
    for raw in block:
        line = raw.strip()
        if line in NOISE:
            continue
        if TEMPLATE_RE.match(line) or any(line.startswith(label + " ") for label in labels):
            return author
        return line
    return author


def parse_predictions(lines: list[str], templates: list[RoundTemplate]) -> list[PredictionRecord]:
    records: list[PredictionRecord] = []
    template_by_round = {template.round_name: template for template in templates}

    index = 0
    while index < len(lines):
        author = parse_author(lines[index])
        if author is None:
            index += 1
            continue

        raw_author, submitted_at = author
        block_start = index + 1
        block_end = block_start
        while block_end < len(lines) and parse_author(lines[block_end]) is None:
            block_end += 1
        block = lines[block_start:block_end]
        participant = moderator_participant(raw_author, block, templates)

        for template in template_by_round.values():
            matched: list[PredictionRecord] = []
            for raw in block:
                line = raw.strip()
                for match in template.matches:
                    score = prediction_score_for_line(line, match)
                    if score is not None:
                        matched.append(
                            PredictionRecord(
                                participant=participant,
                                round_name=template.round_name,
                                position=match.position,
                                score=score,
                                submitted_at=submitted_at,
                                source=f"vk-line-{index + 1}",
                            )
                        )
                        break
            if len(matched) >= 5:
                records.extend(matched)

        index = block_end

    return records


def write_matches(path: Path, templates: list[RoundTemplate]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["round", "position", "home", "away", "kickoff_at", "result", "round_deadline_at"],
        )
        writer.writeheader()
        for template in templates:
            for match in template.matches:
                writer.writerow(
                    {
                        "round": match.round_name,
                        "position": match.position,
                        "home": match.home,
                        "away": match.away,
                        "kickoff_at": "",
                        "result": "",
                        "round_deadline_at": template.deadline_at.isoformat(),
                    }
                )


def write_predictions(path: Path, records: list[PredictionRecord]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["participant", "round", "position", "score", "submitted_at", "source"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "participant": record.participant,
                    "round": record.round_name,
                    "position": record.position,
                    "score": record.score,
                    "submitted_at": record.submitted_at.isoformat(),
                    "source": record.source,
                }
            )


def write_summary(path: Path, templates: list[RoundTemplate], records: list[PredictionRecord]) -> None:
    by_round: dict[str, set[str]] = {template.round_name: set() for template in templates}
    for record in records:
        by_round.setdefault(record.round_name, set()).add(record.participant)

    lines = ["VK parse summary", ""]
    for template in templates:
        lines.append(
            f"Round {template.round_name}: matches={len(template.matches)}, "
            f"deadline={template.deadline_at.isoformat()}, participants={len(by_round[template.round_name])}"
        )
    lines.append("")
    lines.append(f"Prediction rows: {len(records)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_file(source: Path, out_dir: Path) -> tuple[list[RoundTemplate], list[PredictionRecord]]:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    templates = parse_templates(lines)
    records = parse_predictions(lines, templates)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_matches(out_dir / "vk_matches.csv", templates)
    write_predictions(out_dir / "vk_predictions.csv", records)
    write_summary(out_dir / "vk_parse_summary.txt", templates, records)
    return templates, records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse Forecasters Club VK pasted text.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    templates, records = parse_file(args.source, args.out_dir)
    print(f"Parsed rounds={len(templates)}, matches={sum(len(t.matches) for t in templates)}, predictions={len(records)}")
    for template in templates:
        participants = {record.participant for record in records if record.round_name == template.round_name}
        print(f"Round {template.round_name}: matches={len(template.matches)}, participants={len(participants)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
