from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
import sqlite3

from .storage import active_season_id, ingest_prediction_revision
from .variable_sync import resolve_existing_team
from .vk_dry_run import VkForecastSubmission, VkTopicDryRunReport
from .vk_parser import MatchTemplate, RoundTemplate


class VkPredictionImportError(ValueError):
    """The capture cannot be mapped safely to the active EPL database."""


@dataclass(frozen=True)
class VkPredictionImportIssue:
    source_key: str
    participant: str
    round_name: str
    reason: str


@dataclass(frozen=True)
class VkPredictionImportReport:
    group_id: int
    topic_id: int
    submissions_seen: int
    forecasts_seen: int
    revisions_created: int
    duplicates: int
    accepted: int
    rejected: int
    quarantined: int
    issues: tuple[VkPredictionImportIssue, ...]


@dataclass(frozen=True)
class _MappedMatch:
    match_id: int
    position: int
    stable_identity: str
    template: MatchTemplate


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VkPredictionImportError(f"{field} must be timezone-aware")
    return value


def _label_key(value: str) -> str:
    value = value.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", value.strip().casefold())


def stable_vk_comment_key(source_key: str) -> str:
    """Drop Chromium's presentation-order suffix while retaining API IDs.

    API captures already use immutable comment IDs. Public Chromium captures
    use ``author + aware timestamp + ordinal``; the ordinal is presentation
    state and must not create a new revision when VK reorders the DOM.
    """

    value = source_key.strip()
    if value.startswith("vk-api:"):
        if value.rsplit(":", 1)[-1].isdigit():
            return value
        raise VkPredictionImportError("VK API comment key has no numeric comment id")
    if value.startswith("vk:"):
        # Chromium captures include an ordinal only when presentation order is
        # needed to disambiguate otherwise identical rendered comments. A
        # timestamp-and-author key without that suffix is already stable.
        head, separator, suffix = value.rpartition(":")
        if separator and suffix.isdigit() and head and not re.search(r"[+-]\d{2}:\d{2}$", head):
            return head
        return value
    raise VkPredictionImportError("unsupported VK comment key")


def _submission_payload(submission: VkForecastSubmission, stable_comment_key: str) -> dict[str, object]:
    return {
        "stable_comment_key": stable_comment_key,
        "participant": submission.participant,
        "vk_author": submission.vk_author,
        "round_name": submission.round_name,
        "submitted_at": submission.submitted_at.isoformat(),
        "expected_matches": submission.expected_matches,
        "status": submission.status,
        "forecasts": [asdict(item) for item in submission.forecasts],
        "warnings": list(submission.warnings),
    }


def _payload_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _quarantine(
    conn: sqlite3.Connection,
    report: VkTopicDryRunReport,
    submission: VkForecastSubmission,
    *,
    stable_comment_key: str,
    reason: str,
) -> bool:
    payload = _submission_payload(submission, stable_comment_key)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO vk_prediction_quarantine(
            group_id, topic_id, source_key, content_fingerprint,
            participant_name, vk_author, round_name, source_submitted_at,
            observed_at, reason, payload_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(report.group_id),
            int(report.topic_id),
            stable_comment_key,
            _payload_fingerprint(payload),
            submission.participant,
            submission.vk_author,
            submission.round_name,
            submission.submitted_at.isoformat(),
            report.captured_at.isoformat(),
            reason,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    return cursor.rowcount > 0


def _registered_participant(conn: sqlite3.Connection, value: str) -> str | None:
    rows = list(
        conn.execute(
            """
            SELECT p.name
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            WHERE sp.season_id = ?
              AND sp.active = 1
              AND (lower(p.name) = lower(?) OR lower(COALESCE(sp.alias, '')) = lower(?))
            ORDER BY p.id
            """,
            (active_season_id(conn), value.strip(), value.strip()),
        )
    )
    return str(rows[0]["name"]) if len(rows) == 1 else None


def _template_by_round(report: VkTopicDryRunReport) -> dict[str, RoundTemplate]:
    templates: dict[str, RoundTemplate] = {}
    for template in report.templates:
        round_name = template.round_name.strip()
        if round_name in templates:
            raise VkPredictionImportError(f"duplicate VK template for round {round_name}")
        templates[round_name] = template
    return templates


def _map_template(conn: sqlite3.Connection, template: RoundTemplate) -> dict[int, _MappedMatch]:
    rows = list(
        conn.execute(
            """
            SELECT m.id, m.position, m.home, m.away, m.source, m.source_fixture_id
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND r.name = ?
            ORDER BY m.position
            """,
            (active_season_id(conn), template.round_name.strip()),
        )
    )
    if not rows:
        raise VkPredictionImportError(f"round {template.round_name} is absent from the active season")
    if len(rows) != len(template.matches):
        raise VkPredictionImportError(
            f"round {template.round_name} fixture count differs: VK={len(template.matches)} DB={len(rows)}"
        )

    by_pair: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["home"]).casefold(), str(row["away"]).casefold())
        if key in by_pair:
            raise VkPredictionImportError(f"round {template.round_name} has an ambiguous DB team pair")
        if not row["source"] or not row["source_fixture_id"]:
            raise VkPredictionImportError(
                f"round {template.round_name} contains a fixture without stable source identity"
            )
        by_pair[key] = row

    mapped: dict[int, _MappedMatch] = {}
    seen_match_ids: set[int] = set()
    for item in template.matches:
        home = resolve_existing_team(conn, item.home)
        away = resolve_existing_team(conn, item.away)
        row = by_pair.get(((home or "").casefold(), (away or "").casefold()))
        if row is None:
            raise VkPredictionImportError(
                f"round {template.round_name} cannot map VK fixture {item.label!r} by team pair"
            )
        match_id = int(row["id"])
        if item.position in mapped or match_id in seen_match_ids:
            raise VkPredictionImportError(f"round {template.round_name} fixture mapping is not one-to-one")
        seen_match_ids.add(match_id)
        mapped[item.position] = _MappedMatch(
            match_id=match_id,
            position=int(row["position"]),
            stable_identity=f"{row['source']}:{row['source_fixture_id']}",
            template=item,
        )
    if len(mapped) != len(rows):
        raise VkPredictionImportError(f"round {template.round_name} fixture mapping is incomplete")
    return mapped


def import_vk_prediction_report(
    conn: sqlite3.Connection,
    report: VkTopicDryRunReport,
    *,
    expected_group_id: int,
    expected_topic_id: int,
    lock_minutes: int = 90,
) -> VkPredictionImportReport:
    """Project one read-only VK capture into SQLite through immutable revisions.

    Nothing is written to VK. Business projections are written only after the
    configured topic, EPL gate, registered participant and pair-based fixture
    mapping all pass. Unsafe submissions are retained in an idempotent local
    quarantine instead of creating participants or guessing a match position.
    """

    if report.topic_kind != "predictions":
        raise VkPredictionImportError("VK capture is not a predictions topic")
    if (int(report.group_id), int(report.topic_id)) != (int(expected_group_id), int(expected_topic_id)):
        raise VkPredictionImportError("VK capture does not match the configured group/topic")
    if report.league_hint != "epl" or not report.future_ingestion_allowed:
        raise VkPredictionImportError(f"VK predictions topic failed EPL gate: {report.league_hint}")
    observed_at = _aware(report.captured_at, "captured_at")
    templates = _template_by_round(report)

    referenced_rounds = {item.round_name.strip() for item in report.forecast_submissions}
    mapping_by_round: dict[str, dict[int, _MappedMatch] | Exception] = {}
    for round_name in referenced_rounds:
        template = templates.get(round_name)
        if template is None:
            mapping_by_round[round_name] = VkPredictionImportError(f"no VK template for round {round_name}")
            continue
        try:
            mapping_by_round[round_name] = _map_template(conn, template)
        except VkPredictionImportError as exc:
            mapping_by_round[round_name] = exc

    prepared: list[tuple[VkForecastSubmission, str, str]] = []
    identity_groups: dict[tuple[str, str], list[tuple[VkForecastSubmission, str, str]]] = {}
    immediate_issues: list[tuple[VkForecastSubmission, str, str]] = []
    for submission in report.forecast_submissions:
        _aware(submission.submitted_at, "submitted_at")
        try:
            stable_comment = stable_vk_comment_key(submission.source_key)
        except VkPredictionImportError as exc:
            stable_comment = submission.source_key.strip() or "unknown"
            immediate_issues.append((submission, stable_comment, str(exc)))
            continue
        payload_key = _payload_fingerprint(_submission_payload(submission, stable_comment))
        item = (submission, stable_comment, payload_key)
        identity_groups.setdefault((stable_comment, submission.round_name.strip()), []).append(item)

    for items in identity_groups.values():
        distinct_payloads = {item[2] for item in items}
        if len(distinct_payloads) > 1:
            immediate_issues.extend(
                (submission, stable_comment, "ambiguous public VK comment identity")
                for submission, stable_comment, _payload_key in items
            )
            continue
        prepared.append(items[0])

    revisions_created = 0
    duplicates = 0
    accepted = 0
    rejected = 0
    quarantined = 0
    issues: list[VkPredictionImportIssue] = []
    forecasts_seen = sum(len(item.forecasts) for item in report.forecast_submissions)

    conn.execute("SAVEPOINT vk_prediction_import")
    try:
        for submission, stable_comment, reason in immediate_issues:
            if _quarantine(conn, report, submission, stable_comment_key=stable_comment, reason=reason):
                quarantined += 1
            issues.append(
                VkPredictionImportIssue(submission.source_key, submission.participant, submission.round_name, reason)
            )

        for submission, stable_comment, _payload_key in sorted(
            prepared,
            key=lambda item: (item[0].submitted_at, item[0].participant.casefold(), item[1]),
        ):
            round_name = submission.round_name.strip()
            mapping = mapping_by_round.get(round_name)
            if isinstance(mapping, Exception) or mapping is None:
                reason = str(mapping or f"no fixture mapping for round {round_name}")
                if _quarantine(conn, report, submission, stable_comment_key=stable_comment, reason=reason):
                    quarantined += 1
                issues.append(VkPredictionImportIssue(submission.source_key, submission.participant, round_name, reason))
                continue

            forecasts_by_position = {forecast.position: forecast for forecast in submission.forecasts}
            complete = (
                submission.status == "full"
                and len(submission.forecasts) == submission.expected_matches
                and len(forecasts_by_position) == len(submission.forecasts)
                and set(forecasts_by_position) == set(mapping)
                and all(
                    _label_key(forecast.match_label) == _label_key(mapping[position].template.label)
                    for position, forecast in forecasts_by_position.items()
                )
            )
            if not complete:
                reason = "forecast block is incomplete or does not match the VK round template"
                if _quarantine(conn, report, submission, stable_comment_key=stable_comment, reason=reason):
                    quarantined += 1
                issues.append(VkPredictionImportIssue(submission.source_key, submission.participant, round_name, reason))
                continue

            participant = _registered_participant(conn, submission.participant)
            if participant is None:
                reason = "participant is not uniquely registered for the active season"
                if _quarantine(conn, report, submission, stable_comment_key=stable_comment, reason=reason):
                    quarantined += 1
                issues.append(VkPredictionImportIssue(submission.source_key, submission.participant, round_name, reason))
                continue

            stable_prefix = f"vk-prediction:{stable_comment}:round:{round_name}"
            comment_match_prefix = f"{stable_prefix}:match:"
            known_comment = conn.execute(
                """
                SELECT 1
                FROM prediction_revisions
                WHERE source_kind = 'vk'
                  AND substr(stable_source_item_id, 1, ?) = ?
                LIMIT 1
                """,
                (len(comment_match_prefix), comment_match_prefix),
            ).fetchone() is not None
            eligibility_at = observed_at if known_comment else submission.submitted_at

            for forecast in submission.forecasts:
                mapped = mapping.get(forecast.position)
                assert mapped is not None  # Full one-to-one validation above makes this invariant explicit.
                result = ingest_prediction_revision(
                    conn,
                    participant=participant,
                    round_name=round_name,
                    position=mapped.position,
                    score=forecast.raw_score,
                    submitted_at=submission.submitted_at,
                    eligibility_at=eligibility_at,
                    observed_at=observed_at,
                    source=f"vk:{report.group_id}:{report.topic_id}:{stable_comment}",
                    stable_source_item_id=f"{stable_prefix}:match:{mapped.stable_identity}",
                    actor=submission.vk_author,
                    lock_minutes=lock_minutes,
                )
                if not result.created:
                    duplicates += 1
                    continue
                revisions_created += 1
                if result.accepted:
                    accepted += 1
                elif result.decision == "rejected":
                    rejected += 1
                else:
                    quarantined += 1

        conn.execute("RELEASE SAVEPOINT vk_prediction_import")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT vk_prediction_import")
        conn.execute("RELEASE SAVEPOINT vk_prediction_import")
        raise

    return VkPredictionImportReport(
        group_id=int(report.group_id),
        topic_id=int(report.topic_id),
        submissions_seen=len(report.forecast_submissions),
        forecasts_seen=forecasts_seen,
        revisions_created=revisions_created,
        duplicates=duplicates,
        accepted=accepted,
        rejected=rejected,
        quarantined=quarantined,
        issues=tuple(issues),
    )
