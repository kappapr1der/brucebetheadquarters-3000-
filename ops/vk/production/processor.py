#!/usr/bin/env python3
"""Verify a sanitized VK inbox and reconcile it against SQLite without writes."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


CAPTURE_SCHEMA = "brucebet.vk-browser-node-capture/v1"
VERIFICATION_SCHEMA = "brucebet.vk-ssh-pull-verification/v1"
GROUP_ID = 217130885
TOPIC_ID = 67251746
TOPIC_URL = f"https://vk.ru/topic-{GROUP_ID}_{TOPIC_ID}"
HEX64 = re.compile(r"[0-9a-f]{64}")
VISIBLE_TIME = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[\u0430-\u044f\u0451]{3})\s+(?P<year>\d{4})\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
RELATIVE_VISIBLE_TIME = re.compile(
    r"^(?P<day>\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0432\u0447\u0435\u0440\u0430)\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
VK_DISPLAY_ZONE = ZoneInfo("Europe/Moscow")
MONTHS = {
    "\u044f\u043d\u0432": 1,
    "\u0444\u0435\u0432": 2,
    "\u043c\u0430\u0440": 3,
    "\u0430\u043f\u0440": 4,
    "\u043c\u0430\u044f": 5,
    "\u0438\u044e\u043d": 6,
    "\u0438\u044e\u043b": 7,
    "\u0430\u0432\u0433": 8,
    "\u0441\u0435\u043d": 9,
    "\u043e\u043a\u0442": 10,
    "\u043d\u043e\u044f": 11,
    "\u0434\u0435\u043a": 12,
}
RECORD_KEYS = {
    "group_id",
    "topic_id",
    "post_id",
    "canonical_permalink",
    "display_author",
    "public_profile_url",
    "visible_timestamp",
    "body_text",
    "edit_status",
    "observed_at",
}
REQUIRED_MANIFEST_KEYS = {
    "schema",
    "run_id",
    "group_id",
    "topic_id",
    "capture_complete",
    "challenge",
    "pagination_exhausted",
    "stop_reason",
    "displayed_total",
    "record_count",
    "unique_canonical_post_count",
    "newest_post_id",
    "newest_visible_timestamp",
    "artifact_sha256",
    "capture_fingerprint",
    "finished_at",
}
ACCEPTED_DECISIONS = {"accepted", "accepted_partial_late", "manual_override"}
WRITE_ACTIONS = {
    value
    for name in (
        "SQLITE_INSERT",
        "SQLITE_UPDATE",
        "SQLITE_DELETE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_ALTER_TABLE",
        "SQLITE_REINDEX",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
    )
    if (value := getattr(sqlite3, name, None)) is not None
}


class ProcessorError(ValueError):
    pass


@dataclass(frozen=True)
class SourceBundle:
    manifest: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    published_at: dict[int, datetime]
    observed_at: dict[int, datetime]
    artifact_sha256: str
    capture_fingerprint: str


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProcessorError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def parse_aware_iso(value: object, field: str) -> datetime:
    require(isinstance(value, str) and value.strip(), f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProcessorError(f"{field} is not ISO-8601") from exc
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, f"{field} is not timezone-aware")
    return parsed


def parse_visible_timestamp(value: object, observed_at: datetime) -> datetime:
    require(isinstance(value, str), "visible_timestamp is not a string")
    match = VISIBLE_TIME.fullmatch(value.strip())
    if match is not None:
        month = MONTHS.get(match.group("month").casefold())
        require(month is not None, "visible_timestamp has an unknown month")
        hour, minute = (int(item) for item in match.group("time").split(":"))
        # VK renders this wall-clock value in the browser's timezone, which is
        # not present in the sanitized record. Keep it naive until DB-backed calibration.
        return datetime(int(match.group("year")), month, int(match.group("day")), hour, minute)
    relative = RELATIVE_VISIBLE_TIME.fullmatch(value.strip())
    require(relative is not None, "visible_timestamp is not a supported Russian VK timestamp")
    local_date = observed_at.astimezone(VK_DISPLAY_ZONE).date()
    if relative.group("day").casefold() == "\u0432\u0447\u0435\u0440\u0430":
        local_date -= timedelta(days=1)
    hour, minute = (int(item) for item in relative.group("time").split(":"))
    return datetime(local_date.year, local_date.month, local_date.day, hour, minute)


def normalized_visible_timestamp(value: object, observed_at: object) -> str:
    raw = str(value or "").strip()
    match = VISIBLE_TIME.fullmatch(raw)
    if match and (month := MONTHS.get(match.group("month").casefold())):
        hour, minute = (int(item) for item in match.group("time").split(":"))
        wall_time = datetime(
            int(match.group("year")), month, int(match.group("day")), hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    relative = RELATIVE_VISIBLE_TIME.fullmatch(raw)
    if relative:
        observed = parse_aware_iso(observed_at, "observed_at")
        local_date = observed.astimezone(VK_DISPLAY_ZONE).date()
        if relative.group("day").casefold() == "\u0432\u0447\u0435\u0440\u0430":
            local_date -= timedelta(days=1)
        hour, minute = (int(item) for item in relative.group("time").split(":"))
        wall_time = datetime(
            local_date.year, local_date.month, local_date.day, hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    return raw


def logical_fingerprints(records: list[dict[str, Any]]) -> dict[str, str]:
    legacy = []
    absolute_only = []
    normalized = []
    for record in records:
        legacy_item = {key: value for key, value in record.items() if key != "observed_at"}
        absolute_only_item = dict(legacy_item)
        if VISIBLE_TIME.fullmatch(str(absolute_only_item.get("visible_timestamp") or "").strip()):
            absolute_only_item["visible_timestamp"] = normalized_visible_timestamp(
                absolute_only_item.get("visible_timestamp"), record.get("observed_at")
            )
        normalized_item = dict(legacy_item)
        normalized_item["visible_timestamp"] = normalized_visible_timestamp(
            normalized_item.get("visible_timestamp"), record.get("observed_at")
        )
        legacy.append(legacy_item)
        absolute_only.append(absolute_only_item)
        normalized.append(normalized_item)
    suffix = "\n"
    return {
        "normalized-visible-time-v1": sha256((canonical_json(normalized) + suffix).encode("utf-8")),
        "absolute-normalized-relative-raw-v1": sha256(
            (canonical_json(absolute_only) + suffix).encode("utf-8")
        ),
        "legacy-raw-visible-time-v1": sha256((canonical_json(legacy) + suffix).encode("utf-8")),
    }


def parse_checksum_file(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ProcessorError("SHA256SUMS is not ASCII") from exc
    result: dict[str, str] = {}
    for line in lines:
        parts = line.split("  ", 1)
        require(len(parts) == 2 and HEX64.fullmatch(parts[0]) is not None, "invalid SHA256SUMS line")
        require(parts[1] not in result, "duplicate SHA256SUMS entry")
        result[parts[1]] = parts[0]
    require(set(result) == {"capture.jsonl", "manifest.json", "verification.json"}, "unexpected SHA256SUMS inventory")
    return result


def validate_payloads(
    manifest_data: bytes,
    artifact_data: bytes,
    verification_data: bytes,
    sums_data: bytes,
) -> SourceBundle:
    checksums = parse_checksum_file(sums_data)
    require(checksums["manifest.json"] == sha256(manifest_data), "manifest SHA256SUMS mismatch")
    require(checksums["capture.jsonl"] == sha256(artifact_data), "artifact SHA256SUMS mismatch")
    require(checksums["verification.json"] == sha256(verification_data), "verification SHA256SUMS mismatch")

    try:
        manifest = json.loads(manifest_data)
        verification = json.loads(verification_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessorError("manifest or verification JSON is malformed") from exc
    require(isinstance(manifest, dict) and REQUIRED_MANIFEST_KEYS.issubset(manifest), "manifest fields missing")
    require(manifest["schema"] == CAPTURE_SCHEMA, "capture schema mismatch")
    require((manifest["group_id"], manifest["topic_id"]) == (GROUP_ID, TOPIC_ID), "capture topic mismatch")
    require(manifest["capture_complete"] is True, "capture is incomplete")
    require(manifest["challenge"] is False, "capture contains a VK challenge")
    require(manifest["pagination_exhausted"] is True, "pagination exhaustion is not proven")
    require(manifest["stop_reason"] == "displayed_total_exhausted", "capture stop reason is unsafe")
    require(HEX64.fullmatch(str(manifest["artifact_sha256"])) is not None, "artifact hash is invalid")
    require(HEX64.fullmatch(str(manifest["capture_fingerprint"])) is not None, "capture fingerprint is invalid")
    require(manifest["artifact_sha256"] == sha256(artifact_data), "artifact hash mismatch")
    finished_at = parse_aware_iso(manifest["finished_at"], "finished_at")

    require(verification.get("schema") == VERIFICATION_SCHEMA, "transport verification schema mismatch")
    require(verification.get("verified") is True, "transport verification did not pass")
    for key in ("group_id", "topic_id", "artifact_sha256", "capture_fingerprint"):
        require(verification.get(key) == manifest.get(key), f"transport verification mismatch: {key}")

    records: list[dict[str, Any]] = []
    published_at: dict[int, datetime] = {}
    observed_at: dict[int, datetime] = {}
    try:
        raw_lines = artifact_data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ProcessorError("capture JSONL is not UTF-8") from exc
    require(raw_lines and all(line.strip() for line in raw_lines), "capture JSONL contains blank records")
    for line in raw_lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProcessorError("capture JSONL is malformed") from exc
        require(isinstance(record, dict) and set(record) == RECORD_KEYS, "comment field allowlist mismatch")
        require((record["group_id"], record["topic_id"]) == (GROUP_ID, TOPIC_ID), "comment topic mismatch")
        post_id = record["post_id"]
        require(isinstance(post_id, int) and post_id > 0, "invalid post_id")
        require(record["canonical_permalink"] == f"{TOPIC_URL}?post={post_id}", "canonical permalink mismatch")
        profile = urlsplit(str(record["public_profile_url"]))
        require(profile.scheme == "https" and profile.hostname == "vk.ru", "public profile URL is not vk.ru HTTPS")
        require(not profile.query and not profile.fragment and bool(profile.path.strip("/")), "public profile URL is not canonical")
        for key in ("display_author", "visible_timestamp", "body_text", "edit_status", "observed_at"):
            require(isinstance(record[key], str) and record[key].strip(), f"comment field missing: {key}")
        observed_at[post_id] = parse_aware_iso(record["observed_at"], "observed_at")
        published_at[post_id] = parse_visible_timestamp(record["visible_timestamp"], observed_at[post_id])
        if RELATIVE_VISIBLE_TIME.fullmatch(record["visible_timestamp"].strip()):
            published_for_sanity = published_at[post_id].replace(
                tzinfo=VK_DISPLAY_ZONE
            ).astimezone(timezone.utc)
        else:
            # Preserve the prior UTC-host behavior without depending on host TZ.
            published_for_sanity = published_at[post_id].replace(tzinfo=timezone.utc)
        require(observed_at[post_id] >= published_for_sanity, "observed_at predates published_at")
        records.append(record)

    ids = [record["post_id"] for record in records]
    require(ids == sorted(ids), "post IDs are not ordered")
    require(len(ids) == len(set(ids)), "duplicate canonical post IDs")
    expected_count = manifest["displayed_total"]
    require(isinstance(expected_count, int) and expected_count > 0, "displayed_total is invalid")
    for key in ("record_count", "unique_canonical_post_count"):
        require(manifest[key] == expected_count, f"manifest count mismatch: {key}")
    require(len(records) == expected_count, "capture record count mismatch")
    require(verification.get("record_count") == expected_count, "verification record count mismatch")
    require(ids[-1] == manifest["newest_post_id"], "newest post mismatch")
    require(records[-1]["visible_timestamp"] == manifest["newest_visible_timestamp"], "newest timestamp mismatch")
    require(finished_at >= max(observed_at.values()), "manifest finished_at predates observation")

    fingerprints = logical_fingerprints(records)
    require(manifest["capture_fingerprint"] in fingerprints.values(), "logical fingerprint mismatch")
    fingerprint = manifest["capture_fingerprint"]
    return SourceBundle(
        manifest=manifest,
        records=tuple(records),
        published_at=published_at,
        observed_at=observed_at,
        artifact_sha256=sha256(artifact_data),
        capture_fingerprint=fingerprint,
    )


def load_source(inbox: Path) -> SourceBundle:
    inbox = inbox.resolve(strict=True)
    payloads: dict[str, bytes] = {}
    for name in ("manifest.json", "capture.jsonl", "verification.json", "SHA256SUMS"):
        path = inbox / name
        require(path.is_file() and not path.is_symlink(), f"inbox file is missing or unsafe: {name}")
        payloads[name] = path.read_bytes()
    return validate_payloads(
        payloads["manifest.json"],
        payloads["capture.jsonl"],
        payloads["verification.json"],
        payloads["SHA256SUMS"],
    )


def code_api(code_root: Path) -> dict[str, Any]:
    root = str(code_root.resolve(strict=True))
    if root not in sys.path:
        sys.path.insert(0, root)
    from brucebet.scoring import normalize_score, parse_datetime
    from brucebet.storage import _prediction_fingerprint, active_season_id, effective_round_deadline
    from brucebet.vk_dry_run import VkComment, _parse_topic
    from brucebet.vk_parser import parse_templates
    from brucebet.vk_prediction_import import _map_template, _registered_participant

    return {
        "normalize_score": normalize_score,
        "parse_datetime": parse_datetime,
        "prediction_fingerprint": _prediction_fingerprint,
        "active_season_id": active_season_id,
        "effective_round_deadline": effective_round_deadline,
        "VkComment": VkComment,
        "parse_topic": _parse_topic,
        "parse_templates": parse_templates,
        "map_template": _map_template,
        "registered_participant": _registered_participant,
    }


def build_report(bundle: SourceBundle, api: dict[str, Any]):
    comments = []
    templates_by_round: dict[str, Any] = {}
    template_posts: dict[int, list[str]] = {}
    line_cursor = 1
    for record in bundle.records:
        post_id = record["post_id"]
        body_lines = tuple(record["body_text"].splitlines())
        comments.append(
            api["VkComment"](
                source_key=f"vk-public:{GROUP_ID}:{TOPIC_ID}:post:{post_id}",
                author=record["display_author"],
                submitted_at=bundle.published_at[post_id],
                source_line=line_cursor,
                body_lines=body_lines,
            )
        )
        parsed_templates = api["parse_templates"](list(body_lines))
        if parsed_templates:
            template_posts[post_id] = []
        for template in parsed_templates:
            round_name = template.round_name.strip()
            key = (
                template.deadline_at.isoformat(),
                tuple((item.position, item.home, item.away) for item in template.matches),
            )
            previous = templates_by_round.get(round_name)
            if previous is not None:
                previous_key = (
                    previous.deadline_at.isoformat(),
                    tuple((item.position, item.home, item.away) for item in previous.matches),
                )
                require(previous_key == key, f"conflicting templates for round {round_name}")
            else:
                templates_by_round[round_name] = template
            template_posts[post_id].append(round_name)
        line_cursor += len(body_lines) + 1

    templates = tuple(templates_by_round[key] for key in sorted(templates_by_round, key=lambda value: int(value)))
    require(bool(templates), "no round templates recognized")
    source_text = "\n\n".join(record["body_text"] for record in bundle.records)
    report = api["parse_topic"](
        group_id=GROUP_ID,
        topic_id=TOPIC_ID,
        url=TOPIC_URL,
        title="",
        source_text=source_text,
        topic_kind="predictions",
        comments=tuple(comments),
        templates=templates,
        content_fingerprint=bundle.capture_fingerprint,
        capture_complete=True,
        capture_stop_reason=bundle.manifest["stop_reason"],
    )
    captured_at = parse_aware_iso(bundle.manifest["finished_at"], "finished_at")
    report = replace(
        report,
        captured_at=captured_at,
        league_hint="epl",
        capture_score_line_count=sum(len(item.forecasts) for item in report.forecast_submissions),
        capture_warnings=report.capture_warnings
        + ("EPL scope is pinned by configured group/topic identity; no topic title was present in sanitized records.",),
    )
    identities: set[tuple[str, str]] = set()
    for submission in report.forecast_submissions:
        identity = (submission.source_key, submission.round_name.strip())
        require(identity not in identities, "parser produced conflicting versions for one post/round")
        identities.add(identity)
    return report, template_posts


def db_hash(path: Path) -> str:
    return sha256(path.read_bytes())


def open_readonly_db(path: Path) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    uri = path.as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")

    def authorizer(action, _first, _second, _database, _trigger):
        return sqlite3.SQLITE_DENY if action in WRITE_ACTIONS else sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def revision_signature_candidates(
    conn: sqlite3.Connection,
    submission: Any,
    mapping: dict[int, Any],
) -> list[dict[str, Any]]:
    active = rows(
        conn,
        """
        SELECT p.id, p.name
        FROM participants p
        JOIN season_participants sp ON sp.participant_id=p.id
        WHERE sp.season_id=(SELECT id FROM seasons WHERE active=1) AND sp.active=1
        ORDER BY p.id
        """,
    )
    candidates: list[dict[str, Any]] = []
    for participant in active:
        if all(
            conn.execute(
                """
                SELECT 1
                FROM prediction_revisions
                WHERE participant_id=? AND match_id=? AND normalized_score=?
                  AND eligibility_decision IN ('accepted','accepted_partial_late','manual_override')
                LIMIT 1
                """,
                (participant["id"], mapping[forecast.position].match_id, forecast.normalized_score),
            ).fetchone()
            is not None
            for forecast in submission.forecasts
        ) and submission.forecasts:
            candidates.append(participant)
    return candidates


def normalize_visible_instant(value: datetime, offset_minutes: int) -> datetime:
    require(value.tzinfo is None, "source wall time was unexpectedly pre-zoned")
    source_timezone = timezone(timedelta(minutes=offset_minutes))
    return value.replace(tzinfo=source_timezone).astimezone(timezone.utc)


def infer_published_timezone(
    bundle: SourceBundle,
    report: Any,
    mapping_by_round: dict[str, dict[int, Any] | None],
    conn: sqlite3.Connection,
    api: dict[str, Any],
) -> tuple[SourceBundle, dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for submission in report.forecast_submissions:
        mapping = mapping_by_round.get(submission.round_name.strip())
        if mapping is None or not submission.forecasts:
            continue
        post_id = int(submission.source_key.rsplit(":", 1)[-1])
        prefix = f"vk-prediction:{submission.source_key}:round:{submission.round_name.strip()}:match:"
        canonical = rows(
            conn,
            """
            SELECT DISTINCT participant_id
            FROM prediction_revisions
            WHERE source_kind='vk' AND substr(stable_source_item_id, 1, ?) = ?
            ORDER BY participant_id
            """,
            (len(prefix), prefix),
        )
        method = "canonical_source_key"
        if len(canonical) == 1:
            participant_id = int(canonical[0]["participant_id"])
        elif not canonical:
            signature = revision_signature_candidates(conn, submission, mapping)
            if len(signature) != 1:
                continue
            participant_id = int(signature[0]["id"])
            method = "historical_score_signature"
        else:
            continue

        common_times: set[str] | None = None
        for forecast in submission.forecasts:
            available = {
                str(row["source_submitted_at"])
                for row in conn.execute(
                    """
                    SELECT DISTINCT source_submitted_at
                    FROM prediction_revisions
                    WHERE participant_id=? AND match_id=? AND normalized_score=?
                      AND eligibility_decision IN ('accepted','accepted_partial_late','manual_override')
                    """,
                    (participant_id, mapping[forecast.position].match_id, forecast.normalized_score),
                )
                if row["source_submitted_at"]
            }
            common_times = available if common_times is None else common_times & available
        if not common_times or len(common_times) != 1:
            continue
        stored = api["parse_datetime"](next(iter(common_times)))
        require(stored is not None and stored.tzinfo is not None, "stored source timestamp is not timezone-aware")
        source_wall = bundle.published_at[post_id]
        require(source_wall.tzinfo is None, "source wall time was unexpectedly pre-zoned")
        utc_wall = stored.astimezone(timezone.utc).replace(tzinfo=None)
        offset = source_wall - utc_wall
        offset_minutes = int(offset.total_seconds() // 60)
        require(offset == timedelta(minutes=offset_minutes), "source timezone offset is not minute-aligned")
        require(-12 * 60 <= offset_minutes <= 14 * 60, "source timezone offset is invalid")
        evidence.append({"post_id": post_id, "offset_minutes": offset_minutes, "method": method})

    require(len({item["post_id"] for item in evidence}) >= 3, "insufficient evidence to calibrate visible timestamp timezone")
    offsets = {item["offset_minutes"] for item in evidence}
    require(len(offsets) == 1, "visible timestamp timezone calibration is inconsistent")
    offset_minutes = offsets.pop()
    published_at = {
        post_id: normalize_visible_instant(value, offset_minutes)
        for post_id, value in bundle.published_at.items()
    }
    return replace(bundle, published_at=published_at), {
        "method": "existing_revision_instant_calibration",
        "offset_minutes": offset_minutes,
        "evidence_posts": sorted({item["post_id"] for item in evidence}),
        "evidence_count": len({item["post_id"] for item in evidence}),
        "consistent": True,
    }


def same_instant(value: str | None, expected: datetime, api: dict[str, Any]) -> bool:
    if not value:
        return False
    parsed = api["parse_datetime"](value)
    return bool(parsed is not None and parsed.tzinfo is not None and parsed == expected)


def resolve_identity(
    conn: sqlite3.Connection,
    submission: Any,
    mapping: dict[int, Any],
    api: dict[str, Any],
    season_id: int,
) -> dict[str, Any]:
    prefix = f"vk-prediction:{submission.source_key}:round:{submission.round_name.strip()}:match:"
    canonical = rows(
        conn,
        """
        SELECT DISTINCT p.id, p.name
        FROM prediction_revisions revision
        JOIN participants p ON p.id = revision.participant_id
        WHERE revision.source_kind = 'vk'
          AND substr(revision.stable_source_item_id, 1, ?) = ?
        ORDER BY p.id
        """,
        (len(prefix), prefix),
    )
    if len(canonical) == 1:
        return {**canonical[0], "resolved": True, "method": "canonical_source_key"}
    if len(canonical) > 1:
        return {"id": None, "name": None, "resolved": False, "method": "canonical_identity_conflict"}

    registered = api["registered_participant"](conn, submission.participant)
    if registered is not None:
        matched = rows(
            conn,
            """
            SELECT p.id, p.name
            FROM participants p
            JOIN season_participants sp ON sp.participant_id=p.id
            WHERE sp.season_id=? AND sp.active=1 AND p.name=?
            """,
            (season_id, registered),
        )
        if len(matched) == 1:
            return {**matched[0], "resolved": True, "method": "registered_identity"}

    exact = rows(
        conn,
        """
        SELECT p.id, p.name
        FROM participants p
        JOIN season_participants sp ON sp.participant_id=p.id
        WHERE sp.season_id=? AND sp.active=1 AND lower(p.name)=lower(?)
        ORDER BY p.id
        """,
        (season_id, submission.participant.strip()),
    )
    if len(exact) == 1:
        return {**exact[0], "resolved": True, "method": "exact_active_name"}
    if len(exact) > 1:
        return {"id": None, "name": None, "resolved": False, "method": "exact_name_conflict"}

    signature_candidates = revision_signature_candidates(conn, submission, mapping)
    if len(signature_candidates) == 1:
        return {**signature_candidates[0], "resolved": True, "method": "historical_signature"}
    return {
        "id": None,
        "name": None,
        "resolved": False,
        "method": "historical_signature_conflict" if signature_candidates else "unresolved",
    }


def projected_decision(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    round_name: str,
    match_id: int,
    eligibility_at: datetime,
    lock_minutes: int,
    api: dict[str, Any],
) -> tuple[str, str, str | None]:
    deadline = api["effective_round_deadline"](conn, round_name, lock_minutes=lock_minutes)
    row = conn.execute("SELECT kickoff_at FROM matches WHERE id=?", (match_id,)).fetchone()
    require(row is not None, "mapped fixture disappeared")
    kickoff = api["parse_datetime"](row["kickoff_at"]) if row["kickoff_at"] else None
    if deadline is None and kickoff is None:
        return "quarantined", "missing_deadline", None
    current = conn.execute(
        "SELECT 1 FROM predictions WHERE participant_id=? AND match_id=?",
        (participant_id, match_id),
    ).fetchone()
    if deadline is not None and eligibility_at <= deadline:
        return "accepted", "before_round_deadline", deadline.isoformat()
    if current is not None:
        return "rejected", "late_edit", deadline.isoformat() if deadline else None
    if kickoff is not None and eligibility_at < kickoff:
        return "accepted_partial_late", "before_match_kickoff", kickoff.isoformat()
    return "rejected", "late_submission", (kickoff or deadline).isoformat() if (kickoff or deadline) else None


def reconcile_submission(
    conn: sqlite3.Connection,
    submission: Any,
    mapping: dict[int, Any] | None,
    mapping_error: str | None,
    api: dict[str, Any],
    season_id: int,
    lock_minutes: int,
    captured_at: datetime,
    identity_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    post_id = int(submission.source_key.rsplit(":", 1)[-1])
    base = {
        "post_id": post_id,
        "source_key": submission.source_key,
        "round": submission.round_name.strip(),
        "source_participant": submission.participant,
        "vk_author": submission.vk_author,
        "published_at": submission.submitted_at.isoformat(),
        "status": submission.status,
        "forecast_lines": len(submission.forecasts),
        "warnings": list(submission.warnings),
    }
    if mapping_error or mapping is None:
        return {
            **base,
            "participant_id": None,
            "participant_name": None,
            "identity_method": "not_attempted",
            "reconciliation": "quarantine",
            "quarantine_reason": mapping_error or "fixture mapping unavailable",
            "lines": [],
        }
    positions = [item.position for item in submission.forecasts]
    safely_mapped = (
        bool(positions)
        and len(positions) == len(set(positions))
        and set(positions).issubset(mapping)
        and all(
            " ".join(item.match_label.casefold().replace("\u2013", "-").replace("\u2014", "-").split())
            == " ".join(mapping[item.position].template.label.casefold().replace("\u2013", "-").replace("\u2014", "-").split())
            for item in submission.forecasts
        )
    )
    if not safely_mapped:
        return {
            **base,
            "participant_id": None,
            "participant_name": None,
            "identity_method": "not_attempted",
            "reconciliation": "quarantine",
            "quarantine_reason": "forecast block does not match the VK round template",
            "lines": [],
        }

    identity = identity_override or resolve_identity(conn, submission, mapping, api, season_id)
    if not identity["resolved"]:
        return {
            **base,
            "participant_id": None,
            "participant_name": None,
            "identity_method": identity["method"],
            "reconciliation": "quarantine",
            "quarantine_reason": "participant is not uniquely registered or historically linked",
            "lines": [],
        }

    details: list[dict[str, Any]] = []
    for forecast in submission.forecasts:
        mapped = mapping[forecast.position]
        stable_id = (
            f"vk-prediction:{submission.source_key}:round:{submission.round_name.strip()}:"
            f"match:{mapped.stable_identity}"
        )
        fingerprint = api["prediction_fingerprint"](
            identity["id"],
            mapped.match_id,
            forecast.raw_score.strip(),
            submission.submitted_at.isoformat(),
        )
        canonical = rows(
            conn,
            """
            SELECT id, content_fingerprint, eligibility_decision, reason, projected,
                   normalized_score, source_submitted_at, observed_at
            FROM prediction_revisions
            WHERE source_kind='vk' AND stable_source_item_id=?
            ORDER BY id
            """,
            (stable_id,),
        )
        current = conn.execute(
            "SELECT id, score, submitted_at, source FROM predictions WHERE participant_id=? AND match_id=?",
            (identity["id"], mapped.match_id),
        ).fetchone()
        canonical_latest = canonical[-1] if canonical else None
        canonical_semantic_match = bool(
            canonical_latest
            and canonical_latest["normalized_score"] == forecast.normalized_score
            and same_instant(canonical_latest["source_submitted_at"], submission.submitted_at, api)
        )
        if canonical and (canonical_latest["content_fingerprint"] == fingerprint or canonical_semantic_match):
            line_status = "known_canonical"
            decision = canonical_latest["eligibility_decision"]
            reason = canonical_latest["reason"]
            revision_id = canonical_latest["id"]
            deadline_at = None
        elif canonical:
            line_status = "changed"
            decision, reason, deadline_at = projected_decision(
                conn,
                participant_id=identity["id"],
                round_name=submission.round_name.strip(),
                match_id=mapped.match_id,
                eligibility_at=captured_at,
                lock_minutes=lock_minutes,
                api=api,
            )
            revision_id = None
        else:
            legacy = rows(
                conn,
                """
                SELECT id, stable_source_item_id, eligibility_decision, reason, projected,
                       source_submitted_at
                FROM prediction_revisions
                WHERE participant_id=? AND match_id=? AND normalized_score=?
                ORDER BY id
                """,
                (
                    identity["id"],
                    mapped.match_id,
                    forecast.normalized_score,
                ),
            )
            legacy = [
                item
                for item in legacy
                if same_instant(item["source_submitted_at"], submission.submitted_at, api)
            ]
            current_matches = bool(current is not None and current["score"] == forecast.normalized_score)
            compatible_legacy = [
                item
                for item in legacy
                if (
                    current_matches
                    and item["eligibility_decision"] in ACCEPTED_DECISIONS
                )
                or (
                    current is None
                    and item["eligibility_decision"] not in ACCEPTED_DECISIONS
                )
            ]
            latest_legacy = compatible_legacy[-1] if compatible_legacy else None
            if latest_legacy is not None:
                line_status = "known_legacy_equivalent"
                decision = latest_legacy["eligibility_decision"]
                reason = latest_legacy["reason"]
                revision_id = latest_legacy["id"]
                deadline_at = None
            else:
                line_status = "new"
                decision, reason, deadline_at = projected_decision(
                    conn,
                    participant_id=identity["id"],
                    round_name=submission.round_name.strip(),
                    match_id=mapped.match_id,
                    eligibility_at=submission.submitted_at,
                    lock_minutes=lock_minutes,
                    api=api,
                )
                revision_id = None
        details.append(
            {
                "position": forecast.position,
                "match": forecast.match_label,
                "match_id": mapped.match_id,
                "stable_fixture": mapped.stable_identity,
                "raw_score": forecast.raw_score,
                "normalized_score": forecast.normalized_score,
                "stable_source_item_id": stable_id,
                "content_fingerprint": fingerprint,
                "line_status": line_status,
                "projected_decision": decision,
                "projected_reason": reason,
                "projected_deadline_at": deadline_at,
                "existing_revision_id": revision_id,
                "current_prediction_id": int(current["id"]) if current is not None else None,
                "current_score": str(current["score"]) if current is not None else None,
            }
        )

    statuses = {item["line_status"] for item in details}
    if "changed" in statuses:
        reconciliation = "changed"
    elif "new" in statuses:
        reconciliation = "new"
    elif statuses <= {"known_canonical", "known_legacy_equivalent"}:
        reconciliation = "known"
    else:
        reconciliation = "quarantine"
    return {
        **base,
        "participant_id": identity["id"],
        "participant_name": identity["name"],
        "identity_method": identity["method"],
        "reconciliation": reconciliation,
        "quarantine_reason": None,
        "lines": details,
    }


def table_digest(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    data = rows(conn, f'SELECT * FROM "{table}"')
    data.sort(key=lambda item: canonical_json(item))
    return {"count": len(data), "sha256": sha256(pretty_json(data))}


def process(inbox: Path, db_path: Path, code_root: Path) -> dict[str, Any]:
    bundle = load_source(inbox)
    api = code_api(code_root)
    provisional_report, template_posts = build_report(bundle, api)
    db_before = db_hash(db_path)
    conn = open_readonly_db(db_path)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        require(integrity == ["ok"] and not foreign_keys, "backup integrity/FK check failed")
        season_id = api["active_season_id"](conn)
        season = conn.execute(
            "SELECT id, deadline_lock_minutes FROM seasons WHERE id=? AND active=1",
            (season_id,),
        ).fetchone()
        require(season is not None, "active season is unavailable")
        lock_minutes = int(season["deadline_lock_minutes"])
        require(lock_minutes >= 0, "negative deadline lock policy")

        mapping_by_round: dict[str, dict[int, Any] | None] = {}
        mapping_errors: dict[str, str] = {}
        fixtures_by_round: dict[str, int] = {}
        for template in provisional_report.templates:
            round_name = template.round_name.strip()
            try:
                mapping = api["map_template"](conn, template)
                mapping_by_round[round_name] = mapping
                fixtures_by_round[round_name] = len(mapping)
            except Exception as exc:
                mapping_by_round[round_name] = None
                mapping_errors[round_name] = str(exc)
                fixtures_by_round[round_name] = 0

        bundle, timestamp_calibration = infer_published_timezone(
            bundle,
            provisional_report,
            mapping_by_round,
            conn,
            api,
        )
        report, template_posts = build_report(bundle, api)

        provisional_submissions = [
            reconcile_submission(
                conn,
                submission,
                mapping_by_round.get(submission.round_name.strip()),
                mapping_errors.get(submission.round_name.strip()),
                api,
                season_id,
                lock_minutes,
                report.captured_at,
            )
            for submission in report.forecast_submissions
        ]
        records_by_post = {record["post_id"]: record for record in bundle.records}
        profile_candidates: dict[str, set[tuple[int, str]]] = {}
        for item in provisional_submissions:
            if item["participant_id"] is None:
                continue
            profile = records_by_post[item["post_id"]]["public_profile_url"]
            profile_candidates.setdefault(profile, set()).add(
                (int(item["participant_id"]), str(item["participant_name"]))
            )
        profile_identities = {
            profile: next(iter(candidates))
            for profile, candidates in profile_candidates.items()
            if len(candidates) == 1
        }
        submissions: list[dict[str, Any]] = []
        for submission, provisional in zip(report.forecast_submissions, provisional_submissions, strict=True):
            if provisional["participant_id"] is not None:
                submissions.append(provisional)
                continue
            post_id = int(submission.source_key.rsplit(":", 1)[-1])
            linked = profile_identities.get(records_by_post[post_id]["public_profile_url"])
            if linked is None:
                submissions.append(provisional)
                continue
            submissions.append(
                reconcile_submission(
                    conn,
                    submission,
                    mapping_by_round.get(submission.round_name.strip()),
                    mapping_errors.get(submission.round_name.strip()),
                    api,
                    season_id,
                    lock_minutes,
                    report.captured_at,
                    identity_override={
                        "id": linked[0],
                        "name": linked[1],
                        "resolved": True,
                        "method": "source_profile_link",
                    },
                )
            )

        by_post: dict[int, list[dict[str, Any]]] = {}
        for item in submissions:
            by_post.setdefault(item["post_id"], []).append(item)
        post_rows: list[dict[str, Any]] = []
        for record in bundle.records:
            post_id = record["post_id"]
            items = by_post.get(post_id, [])
            if post_id in template_posts and items:
                classification = "template_and_forecast"
            elif post_id in template_posts:
                classification = "template"
            elif items:
                classification = "forecast"
            elif record["display_author"] == "Forecasters Club":
                classification = "admin"
            else:
                classification = "other"
            post_rows.append(
                {
                    "post_id": post_id,
                    "source_key": f"vk-public:{GROUP_ID}:{TOPIC_ID}:post:{post_id}",
                    "permalink": record["canonical_permalink"],
                    "author": record["display_author"],
                    "profile_url": record["public_profile_url"],
                    "published_at": bundle.published_at[post_id].isoformat(),
                    "observed_at": bundle.observed_at[post_id].isoformat(),
                    "edit_status": record["edit_status"],
                    "classification": classification,
                    "template_rounds": template_posts.get(post_id, []),
                    "forecast_rounds": [item["round"] for item in items],
                    "reconciliation": [item["reconciliation"] for item in items],
                }
            )

        round_rows: list[dict[str, Any]] = []
        for template in report.templates:
            round_name = template.round_name.strip()
            items = [item for item in submissions if item["round"] == round_name]
            lines = [line for item in items for line in item["lines"]]
            completed_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN m.result IS NOT NULL AND trim(m.result)<>'' THEN 1 ELSE 0 END) AS finished
                FROM matches m JOIN rounds r ON r.id=m.round_id
                WHERE r.season_id=? AND r.name=?
                """,
                (season_id, round_name),
            ).fetchone()
            total = int(completed_row["total"] or 0)
            finished = int(completed_row["finished"] or 0)
            round_rows.append(
                {
                    "round": round_name,
                    "template_post_ids": [post for post, values in template_posts.items() if round_name in values],
                    "fixtures_in_template": len(template.matches),
                    "fixtures_mapped": fixtures_by_round.get(round_name, 0),
                    "fixture_mapping_error": mapping_errors.get(round_name),
                    "db_fixtures_finished": finished,
                    "db_round_complete": total > 0 and total == finished,
                    "forecast_submissions": len(items),
                    "participants_resolved": len({item["participant_id"] for item in items if item["participant_id"] is not None}),
                    "unknown_participants": len([item for item in items if item["participant_id"] is None]),
                    "known_comments": len([item for item in items if item["reconciliation"] == "known"]),
                    "new_comments": len([item for item in items if item["reconciliation"] == "new"]),
                    "changed_comments": len([item for item in items if item["reconciliation"] == "changed"]),
                    "quarantine_candidates": len([item for item in items if item["reconciliation"] == "quarantine"]),
                    "forecast_lines": sum(item["forecast_lines"] for item in items),
                    "known_canonical_lines": len([line for line in lines if line["line_status"] == "known_canonical"]),
                    "known_legacy_lines": len([line for line in lines if line["line_status"] == "known_legacy_equivalent"]),
                    "projected_revision_lines": len([line for line in lines if line["line_status"] in {"new", "changed"}]),
                    "projected_accepted_lines": len([line for line in lines if line["line_status"] in {"new", "changed"} and line["projected_decision"] in ACCEPTED_DECISIONS]),
                    "projected_rejected_lines": len([line for line in lines if line["line_status"] in {"new", "changed"} and line["projected_decision"] == "rejected"]),
                }
            )

        completed_rounds = {row["round"] for row in round_rows if row["db_round_complete"]}
        restored_rounds = {
            str(row["round"])
            for row in conn.execute(
                """
                SELECT DISTINCT r.name AS round
                FROM prediction_revisions revision
                JOIN matches m ON m.id=revision.match_id
                JOIN rounds r ON r.id=m.round_id
                WHERE revision.source_kind='vk' AND r.season_id=?
                """,
                (season_id,),
            )
        }
        historical_items = [item for item in submissions if item["round"] in restored_rounds]
        historical_noop = all(item["reconciliation"] == "known" for item in historical_items)
        round2_items = [item for item in submissions if item["round"] == "2"]
        round2_noop = bool(round2_items) and all(item["reconciliation"] == "known" for item in round2_items)
        new_changed = [item for item in submissions if item["reconciliation"] in {"new", "changed"}]
        quarantined = [item for item in submissions if item["reconciliation"] == "quarantine"]
        projected_lines = [
            line
            for item in new_changed
            for line in item["lines"]
            if line["line_status"] in {"new", "changed"}
        ]
        frozen = rows(conn, "SELECT * FROM contest_recommendations WHERE frozen_final=1 ORDER BY id")
        protected = {
            name: table_digest(conn, name)
            for name in ("contest_recommendations", "round_reviews", "prediction_revisions", "predictions")
        }
        result = {
            "schema": "brucebet.sanitized-inbox-dry-run/v1",
            "source": {
                "group_id": GROUP_ID,
                "topic_id": TOPIC_ID,
                "run_id": bundle.manifest["run_id"],
                "capture_fingerprint": bundle.capture_fingerprint,
                "artifact_sha256": bundle.artifact_sha256,
                "record_count": len(bundle.records),
                "newest_post_id": bundle.manifest["newest_post_id"],
                "newest_visible_timestamp": bundle.manifest["newest_visible_timestamp"],
                "capture_complete": True,
                "published_timestamp_calibration": timestamp_calibration,
            },
            "backup": {
                "sha256": db_before,
                "integrity_check": integrity,
                "foreign_key_violations": len(foreign_keys),
                "opened_mode": "ro+immutable+query_only",
                "total_changes": conn.total_changes,
            },
            "parser": {
                "templates": len(report.templates),
                "forecast_submissions": len(report.forecast_submissions),
                "forecast_lines": report.capture_score_line_count,
                "comments": len(report.comments),
                "source_keys_stable": all(item.source_key.startswith(f"vk-public:{GROUP_ID}:{TOPIC_ID}:post:") for item in report.comments),
                "observed_at_used_as_published_at": False,
                "edit_history_invented": False,
                "profile_identity_links": len(profile_identities),
            },
            "policy": {"active_season_id": season_id, "lock_minutes": lock_minutes},
            "per_post": post_rows,
            "submissions": submissions,
            "per_round": round_rows,
            "new_changed_comments": [
                {
                    "post_id": item["post_id"],
                    "source_key": item["source_key"],
                    "round": item["round"],
                    "participant": item["participant_name"],
                    "source_participant": item["source_participant"],
                    "reconciliation": item["reconciliation"],
                    "projected_revision_lines": len([line for line in item["lines"] if line["line_status"] in {"new", "changed"}]),
                }
                for item in new_changed
            ],
            "projected_import": {
                "revision_lines": len(projected_lines),
                "accepted_lines": len([line for line in projected_lines if line["projected_decision"] in ACCEPTED_DECISIONS]),
                "rejected_lines": len([line for line in projected_lines if line["projected_decision"] == "rejected"]),
                "quarantined_lines": len([line for line in projected_lines if line["projected_decision"] == "quarantined"]),
                "quarantine_submissions": [
                    {
                        "post_id": item["post_id"],
                        "source_key": item["source_key"],
                        "round": item["round"],
                        "source_participant": item["source_participant"],
                        "reason": item["quarantine_reason"],
                    }
                    for item in quarantined
                ],
            },
            "historical_safety": {
                "completed_rounds": sorted(completed_rounds, key=lambda value: int(value)),
                "protected_vk_history_rounds": sorted(restored_rounds, key=lambda value: int(value)),
                "historical_rounds_noop": historical_noop,
                "round2_noop": round2_noop,
                "round2_submissions": len(round2_items),
                "frozen_recommendations_count": len(frozen),
                "frozen_recommendations_sha256": sha256(pretty_json(frozen)),
                "protected_table_digests": protected,
                "old_revisions_rewritten": False,
                "frozen_recommendations_recomputed": False,
            },
            "flags": {
                "inbox_processor_ready": True,
                "source_validation_pass": True,
                "parser_bridge_pass": bool(report.templates),
                "production_copy_reconciliation_pass": not mapping_errors,
                "historical_rounds_noop": historical_noop,
                "new_changes_detected": len({item["post_id"] for item in new_changed}),
                "import_candidate": bool(new_changed) and not quarantined and not mapping_errors,
                "scheduler_candidate": bool(report.templates) and not mapping_errors and historical_noop,
            },
        }
    finally:
        conn.close()
    db_after = db_hash(db_path)
    require(db_after == db_before, "database copy changed during pure dry-run")
    result["backup"]["sha256_after"] = db_after
    result["backup"]["physical_noop"] = True
    return result


def write_csv(path: Path, rows_data: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in rows_data:
            row = dict(item)
            for key, value in row.items():
                if isinstance(value, (list, dict)):
                    row[key] = canonical_json(value)
            writer.writerow(row)


def write_outputs(output: Path, result: dict[str, Any]) -> dict[str, str]:
    require(not output.exists(), "output directory already exists")
    output.mkdir(parents=True, mode=0o700)
    (output / "dry-run-result.json").write_bytes(pretty_json(result))
    (output / "new-changed-comments.json").write_bytes(pretty_json(result["new_changed_comments"]))
    (output / "projected-import-quarantine.json").write_bytes(pretty_json(result["projected_import"]))
    write_csv(
        output / "per-post-reconciliation.csv",
        result["per_post"],
        [
            "post_id",
            "source_key",
            "permalink",
            "author",
            "profile_url",
            "published_at",
            "observed_at",
            "edit_status",
            "classification",
            "template_rounds",
            "forecast_rounds",
            "reconciliation",
        ],
    )
    write_csv(
        output / "per-round-summary.csv",
        result["per_round"],
        [
            "round",
            "template_post_ids",
            "fixtures_in_template",
            "fixtures_mapped",
            "fixture_mapping_error",
            "db_fixtures_finished",
            "db_round_complete",
            "forecast_submissions",
            "participants_resolved",
            "unknown_participants",
            "known_comments",
            "new_comments",
            "changed_comments",
            "quarantine_candidates",
            "forecast_lines",
            "known_canonical_lines",
            "known_legacy_lines",
            "projected_revision_lines",
            "projected_accepted_lines",
            "projected_rejected_lines",
        ],
    )
    hashes: dict[str, str] = {}
    for path in sorted(output.iterdir(), key=lambda item: item.name):
        if path.is_file():
            hashes[path.name] = sha256(path.read_bytes())
    sums = "".join(f"{value}  {name}\n" for name, value in hashes.items()).encode("ascii")
    (output / "SHA256SUMS").write_bytes(sums)
    return hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = process(args.inbox, args.db, args.code_root)
    hashes = write_outputs(args.output, result)
    print(
        canonical_json(
            {
                "flags": result["flags"],
                "output_hashes": hashes,
                "source_fingerprint": result["source"]["capture_fingerprint"],
                "backup_sha256": result["backup"]["sha256"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
