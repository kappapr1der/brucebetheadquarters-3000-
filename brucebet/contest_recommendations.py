from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from typing import Iterable, Mapping

from .analytics import intelligence_readiness, match_rows_for_round, round_deadlines, strategy_summary
from .scoring import normalize_score, parse_datetime, parse_score
from .storage import active_season_id


ALGORITHM_VERSION = "contest-v1"
MAX_TELEGRAM_TEXT = 3900
OUTCOMES = ("P1", "X", "P2")


@dataclass(frozen=True)
class ContestRecommendation:
    id: int
    match_id: int
    round_name: str
    position: int
    home: str
    away: str
    recommended_score: str
    recommended_outcome: str
    status: str
    confidence: float | None
    risk_level: str | None
    field_prediction_count: int
    field_expected_count: int
    field_top_outcome: str | None
    field_top_share: float | None
    market_present: bool
    strategy_mode: str
    readiness_status: str
    generated_at: str
    frozen_final: bool
    input_fingerprint: str
    previous_recommendation_id: int | None


@dataclass(frozen=True)
class ContestRecommendationBatch:
    season_id: int
    round_id: int
    round_name: str
    recommendations: tuple[ContestRecommendation, ...]
    changed: tuple[tuple[ContestRecommendation | None, ContestRecommendation], ...]
    field_complete_count: int
    field_expected_count: int
    readiness_status: str
    market_present_count: int
    generated_at: str
    deadline_at: datetime | None
    frozen_final: bool
    recomputed: bool
    deadline_locked: bool

    @property
    def fingerprint(self) -> str:
        payload = {
            "algorithm": ALGORITHM_VERSION,
            "round_id": self.round_id,
            "frozen_final": self.frozen_final,
            "recommendations": [
                {
                    "match_id": item.match_id,
                    "score": item.recommended_score,
                    "status": item.status,
                    "input": item.input_fingerprint,
                }
                for item in self.recommendations
            ],
        }
        return _fingerprint(payload)


@dataclass(frozen=True)
class ContestRecommendationNotificationDelivery:
    event_key: str
    chat_id: int
    text: str


def _fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _outcome(score: str | None) -> str | None:
    parsed = parse_score(score)
    if parsed is None:
        return None
    if parsed.outcome > 0:
        return "P1"
    if parsed.outcome < 0:
        return "P2"
    return "X"


def _normalise_distribution(values: Mapping[str, object]) -> dict[str, float]:
    prepared: dict[str, float] = {}
    for label in OUTCOMES:
        try:
            value = float(values.get(label, 0.0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            prepared[label] = value
    total = sum(prepared.values())
    if total <= 0:
        return {}
    return {label: value / total for label, value in prepared.items()}


def _top(values: Mapping[str, float]) -> tuple[str | None, float | None]:
    if not values:
        return None, None
    label, value = sorted(values.items(), key=lambda item: (-item[1], item[0]))[0]
    return label, round(value, 4)


def _score_top(values: Counter[str]) -> tuple[str | None, float | None]:
    total = sum(values.values())
    if total == 0:
        return None, None
    label, value = sorted(values.items(), key=lambda item: (-item[1], item[0]))[0]
    return label, round(value / total, 4)


def _latest_market(conn: sqlite3.Connection, match_id: int) -> tuple[sqlite3.Row | None, dict[str, float]]:
    row = conn.execute(
        "SELECT * FROM match_odds WHERE match_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (match_id,),
    ).fetchone()
    if row is None:
        return None, {}
    raw: dict[str, float] = {}
    for label, column in (("P1", "home_win"), ("X", "draw"), ("P2", "away_win")):
        try:
            odds = float(row[column])
        except (TypeError, ValueError):
            continue
        if odds > 0:
            raw[label] = 1.0 / odds
    return row, _normalise_distribution(raw)


def _field_signal(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    season_id: int,
    user_participant: str,
) -> dict[str, object]:
    roster = list(
        conn.execute(
            """
            SELECT p.id, p.name
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            WHERE sp.season_id = ? AND sp.active = 1
            ORDER BY p.name
            """,
            (season_id,),
        )
    )
    eligible_ids = {
        int(row["id"])
        for row in roster
        if str(row["name"]).casefold() != user_participant.casefold()
    }
    rows = list(
        conn.execute(
            """
            SELECT pr.participant_id, pr.score
            FROM predictions pr
            JOIN season_participants sp ON sp.participant_id = pr.participant_id
            WHERE pr.match_id = ? AND sp.season_id = ? AND sp.active = 1
            """,
            (match_id, season_id),
        )
    )
    scores: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    participants: set[int] = set()
    for row in rows:
        participant_id = int(row["participant_id"])
        if participant_id not in eligible_ids:
            continue
        score = parse_score(row["score"])
        if score is None:
            continue
        participants.add(participant_id)
        label = score.label()
        scores[label] += 1
        outcomes[_outcome(label) or "X"] += 1
    distribution = _normalise_distribution(outcomes)
    top_outcome, top_share = _top(distribution)
    top_score, top_score_share = _score_top(scores)
    return {
        "prediction_count": len(participants),
        "expected_count": len(eligible_ids),
        "scores": dict(sorted(scores.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "probabilities": distribution,
        "top_outcome": top_outcome,
        "top_share": top_share,
        "top_score": top_score,
        "top_score_share": top_score_share,
    }


def _round_field_completeness(
    conn: sqlite3.Connection,
    *,
    matches: list[sqlite3.Row],
    season_id: int,
    user_participant: str,
) -> tuple[int, int]:
    roster = list(
        conn.execute(
            """
            SELECT p.id, p.name
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            WHERE sp.season_id = ? AND sp.active = 1
            """,
            (season_id,),
        )
    )
    expected_ids = {
        int(row["id"])
        for row in roster
        if str(row["name"]).casefold() != user_participant.casefold()
    }
    if not expected_ids:
        return 0, 0
    match_ids = [int(item["id"]) for item in matches]
    placeholders = ", ".join("?" for _ in match_ids)
    rows = conn.execute(
        f"""
        SELECT participant_id, match_id, score
        FROM predictions
        WHERE match_id IN ({placeholders})
        """,
        match_ids,
    ).fetchall()
    submitted: dict[int, set[int]] = {participant_id: set() for participant_id in expected_ids}
    for row in rows:
        participant_id = int(row["participant_id"])
        if participant_id in submitted and parse_score(row["score"]) is not None:
            submitted[participant_id].add(int(row["match_id"]))
    required_matches = set(match_ids)
    complete = sum(required_matches.issubset(items) for items in submitted.values())
    return complete, len(expected_ids)


def _effective_deadline(conn: sqlite3.Connection, round_name: str, lock_minutes: int) -> datetime | None:
    for item in round_deadlines(conn, lock_minutes=lock_minutes):
        if item.round_name == round_name:
            return item.effective_deadline_at
    return None


def _strategy_mode(conn: sqlite3.Connection, user_participant: str, lock_minutes: int) -> tuple[str, str]:
    finished = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ? AND m.result IS NOT NULL
        """,
        (active_season_id(conn),),
    ).fetchone()
    if finished is None or int(finished["count"]) == 0:
        return "balanced", "opening_round_neutral"
    strategy = strategy_summary(conn, user_participant=user_participant, lock_minutes=lock_minutes)
    return str(strategy["mode"]), "standings"


def _weighted_outcome(
    *,
    model: Mapping[str, float],
    market: Mapping[str, float],
    field: Mapping[str, float],
    strategy_mode: str,
    field_top: str | None,
    field_share: float | None,
) -> tuple[str | None, dict[str, float]]:
    sources = ((model, 0.55), (market, 0.25), (field, 0.20))
    present = [(values, weight) for values, weight in sources if values]
    if not present:
        return None, {}
    total_weight = sum(weight for _values, weight in present)
    composite = {label: 0.0 for label in OUTCOMES}
    for values, weight in present:
        for label, value in values.items():
            composite[label] += (weight / total_weight) * value

    model_top, _model_share = _top(model)
    market_top, _market_share = _top(market)
    if strategy_mode == "protect" and field_top and (field_share or 0) >= 0.65:
        composite[field_top] += 0.10
    elif strategy_mode in {"chase", "aggressive"} and model_top and market_top and field_top:
        if model_top == market_top and model_top != field_top:
            composite[model_top] += 0.08 if strategy_mode == "chase" else 0.12
    composite = _normalise_distribution(composite)
    return _top(composite)[0], composite


def _canonical_score(outcome: str) -> str:
    return {"P1": "1:0", "P2": "0:1", "X": "1:1"}.get(outcome, "1:1")


def _choose_exact_score(
    *,
    final_outcome: str,
    base_score: str | None,
    field_top_score: str | None,
    field_top_score_share: float | None,
    field_top_outcome: str | None,
    market_top_outcome: str | None,
    volatility: float,
) -> str:
    base = normalize_score(base_score)
    if base and _outcome(base) == final_outcome:
        selected = base
        if (
            field_top_score
            and _outcome(field_top_score) == final_outcome
            and (field_top_score_share or 0) >= 0.60
            and (field_top_outcome == final_outcome or market_top_outcome == final_outcome)
        ):
            selected = field_top_score
    elif field_top_score and _outcome(field_top_score) == final_outcome and (field_top_score_share or 0) >= 0.50:
        selected = field_top_score
    else:
        selected = _canonical_score(final_outcome)

    parsed = parse_score(selected)
    if parsed is not None and volatility >= 0.65 and abs(parsed.home - parsed.away) > 1:
        return _canonical_score(final_outcome)
    return selected


def _recommendation_from_row(row: sqlite3.Row) -> ContestRecommendation:
    return ContestRecommendation(
        id=int(row["id"]),
        match_id=int(row["match_id"]),
        round_name=str(row["round_name"]),
        position=int(row["position"]),
        home=str(row["home"]),
        away=str(row["away"]),
        recommended_score=str(row["recommended_score"]),
        recommended_outcome=str(row["recommended_outcome"]),
        status=str(row["status"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        risk_level=str(row["risk_level"]) if row["risk_level"] else None,
        field_prediction_count=int(row["field_prediction_count"]),
        field_expected_count=int(row["field_expected_count"]),
        field_top_outcome=str(row["field_top_outcome"]) if row["field_top_outcome"] else None,
        field_top_share=float(row["field_top_share"]) if row["field_top_share"] is not None else None,
        market_present=bool(row["market_present"]),
        strategy_mode=str(row["strategy_mode"]),
        readiness_status=str(row["readiness_status"]),
        generated_at=str(row["generated_at"]),
        frozen_final=bool(row["frozen_final"]),
        input_fingerprint=str(row["input_fingerprint"]),
        previous_recommendation_id=(
            int(row["previous_recommendation_id"])
            if row["previous_recommendation_id"] is not None
            else None
        ),
    )


def _latest_recommendation_row(conn: sqlite3.Connection, match_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contest_recommendations WHERE match_id = ? ORDER BY id DESC LIMIT 1",
        (match_id,),
    ).fetchone()


def _record_row(conn: sqlite3.Connection, record_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contest_recommendations WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"missing contest recommendation {record_id}")
    return row


def _round_recommendations(conn: sqlite3.Connection, round_id: int) -> tuple[ContestRecommendation, ...]:
    rows = conn.execute(
        """
        SELECT recommendation.*
        FROM contest_recommendations recommendation
        JOIN (
            SELECT match_id, MAX(id) AS id
            FROM contest_recommendations
            WHERE round_id = ?
            GROUP BY match_id
        ) latest ON latest.id = recommendation.id
        ORDER BY recommendation.position
        """,
        (round_id,),
    ).fetchall()
    return tuple(_recommendation_from_row(row) for row in rows)


def _round_has_final_snapshot(conn: sqlite3.Connection, round_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM contest_recommendations WHERE round_id = ? AND frozen_final = 1 LIMIT 1",
        (round_id,),
    ).fetchone()
    return row is not None


def recompute_contest_recommendations(
    conn: sqlite3.Connection,
    *,
    round_name: str | None = None,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
    now: datetime | None = None,
    finalize: bool = False,
    notification_chat_ids: Iterable[int] = (),
    enqueue_update: bool = False,
) -> ContestRecommendationBatch:
    """Build the auditable contest pick without mutating the independent model.

    Model weights are 55%, market 25%, and the eligible competitor field 20%,
    normalized across present sources. Strategy only adds a documented bounded
    adjustment; it never changes the stored football assessment.
    """

    generated = now or datetime.now().astimezone()
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("Contest recommendation synthesis requires an aware timestamp")
    matches = match_rows_for_round(conn, round_name)
    if not matches:
        raise ValueError("No active round fixtures are available for contest recommendations")
    resolved_round_name = str(matches[0]["round_name"])
    round_id = int(matches[0]["round_id"])
    season_id = active_season_id(conn)
    deadline_at = _effective_deadline(conn, resolved_round_name, lock_minutes)
    if deadline_at is not None and generated.astimezone(timezone.utc) >= deadline_at.astimezone(timezone.utc):
        existing = _round_recommendations(conn, round_id)
        complete_count, expected_count = _round_field_completeness(
            conn, matches=matches, season_id=season_id, user_participant=user_participant
        )
        return ContestRecommendationBatch(
            season_id=season_id,
            round_id=round_id,
            round_name=resolved_round_name,
            recommendations=existing,
            changed=(),
            field_complete_count=complete_count,
            field_expected_count=expected_count,
            readiness_status="locked",
            market_present_count=sum(item.market_present for item in existing),
            generated_at=generated.isoformat(),
            deadline_at=deadline_at,
            frozen_final=_round_has_final_snapshot(conn, round_id),
            recomputed=False,
            deadline_locked=True,
        )

    readiness = intelligence_readiness(conn, resolved_round_name, now=generated, lock_minutes=lock_minutes)
    readiness_by_match = {int(item["match"]["id"]): item for item in readiness["items"]}
    strategy_mode, strategy_source = _strategy_mode(conn, user_participant, lock_minutes)
    field_complete_count, field_expected_count = _round_field_completeness(
        conn,
        matches=matches,
        season_id=season_id,
        user_participant=user_participant,
    )
    frozen_final = bool(finalize or _round_has_final_snapshot(conn, round_id))
    changed: list[tuple[ContestRecommendation | None, ContestRecommendation]] = []

    for match in matches:
        match_id = int(match["id"])
        assessment = conn.execute("SELECT * FROM match_assessments WHERE match_id = ?", (match_id,)).fetchone()
        base_score = normalize_score(assessment["suggested_score"]) if assessment else None
        base_outcome = _outcome(base_score)
        model = _normalise_distribution(
            {
                "P1": assessment["home_edge"] if assessment else None,
                "X": assessment["draw_edge"] if assessment else None,
                "P2": assessment["away_edge"] if assessment else None,
            }
        )
        if not model and base_outcome:
            model = {base_outcome: 1.0}
        field = _field_signal(
            conn,
            match_id=match_id,
            season_id=season_id,
            user_participant=user_participant,
        )
        odds, market = _latest_market(conn, match_id)
        market_top, market_share = _top(market)
        volatility = 0.0
        if assessment is not None:
            try:
                volatility = max(0.0, min(1.0, float(assessment["volatility"])))
            except (TypeError, ValueError):
                pass
        final_outcome, composite = _weighted_outcome(
            model=model,
            market=market,
            field=field["probabilities"],
            strategy_mode=strategy_mode,
            field_top=field["top_outcome"],
            field_share=field["top_share"],
        )
        readiness_item = readiness_by_match.get(match_id)
        readiness_status = str(readiness_item["status"]) if readiness_item else "blocked"
        warnings = [
            f"{item['key']}:{item['state']}"
            for item in (readiness_item["follow_up"] if readiness_item else [])
        ]
        if not market:
            warnings.append("market:missing")
        if field["prediction_count"] < field["expected_count"]:
            warnings.append(f"field:incomplete:{field['prediction_count']}/{field['expected_count']}")
        if not base_score or not final_outcome:
            status = "blocked"
            recommended_score = ""
            recommended_outcome = ""
            warnings.append("model:missing_assessment")
        else:
            recommended_score = _choose_exact_score(
                final_outcome=final_outcome,
                base_score=base_score,
                field_top_score=field["top_score"],
                field_top_score_share=field["top_score_share"],
                field_top_outcome=field["top_outcome"],
                market_top_outcome=market_top,
                volatility=volatility,
            )
            recommended_outcome = final_outcome
            if readiness_status == "blocked":
                status = "blocked"
            elif readiness_status != "ready" or field_complete_count < field_expected_count:
                status = "provisional"
            elif frozen_final:
                status = "final"
            else:
                status = "ready"
        model_confidence = None
        if assessment is not None:
            try:
                model_confidence = max(0.0, min(1.0, float(assessment["confidence"])))
            except (TypeError, ValueError):
                pass
        selected_probability = composite.get(recommended_outcome, 0.0)
        confidence = round(
            0.5 * selected_probability + 0.5 * (model_confidence if model_confidence is not None else selected_probability),
            3,
        ) if recommended_outcome else None
        risk_level = str(assessment["risk_level"]) if assessment and assessment["risk_level"] else None
        if volatility >= 0.65:
            risk_level = "high"
        input_payload: dict[str, object] = {
            "algorithm": ALGORITHM_VERSION,
            "match_id": match_id,
            "base_model": {
                "score": base_score,
                "probabilities": model,
                "confidence": model_confidence,
                "risk_level": risk_level,
                "volatility": volatility,
                "assessment_updated_at": assessment["updated_at"] if assessment else None,
            },
            "field": field,
            "market": {
                "captured_at": odds["captured_at"] if odds else None,
                "probabilities": market,
                "top_outcome": market_top,
                "top_share": market_share,
            },
            "strategy": {"mode": strategy_mode, "source": strategy_source},
            "readiness": {"status": readiness_status, "warnings": warnings},
            "frozen_final": frozen_final,
        }
        input_fingerprint = _fingerprint(input_payload)
        previous_row = _latest_recommendation_row(conn, match_id)
        existing = conn.execute(
            """
            SELECT * FROM contest_recommendations
            WHERE match_id = ? AND input_fingerprint = ? AND frozen_final = ?
            ORDER BY id DESC LIMIT 1
            """,
            (match_id, input_fingerprint, int(frozen_final)),
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                """
                INSERT INTO contest_recommendations(
                    season_id, round_id, match_id, round_name, position, home, away,
                    recommended_score, recommended_outcome, status, confidence, risk_level,
                    model_suggested_score, model_probabilities_json, model_assessment_updated_at,
                    field_prediction_count, field_expected_count, field_scores_json, field_outcomes_json,
                    field_top_outcome, field_top_share, field_top_scores_json,
                    market_present, market_captured_at, market_probabilities_json, market_top_outcome,
                    market_top_share, strategy_mode, volatility, readiness_status, readiness_warnings_json,
                    input_fingerprint, generated_at, frozen_final, freeze_reason, previous_recommendation_id
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    season_id,
                    round_id,
                    match_id,
                    resolved_round_name,
                    int(match["position"]),
                    str(match["home"]),
                    str(match["away"]),
                    recommended_score,
                    recommended_outcome,
                    status,
                    confidence,
                    risk_level,
                    base_score,
                    json.dumps(model, sort_keys=True),
                    assessment["updated_at"] if assessment else None,
                    field["prediction_count"],
                    field["expected_count"],
                    json.dumps(field["scores"], sort_keys=True),
                    json.dumps(field["outcomes"], sort_keys=True),
                    field["top_outcome"],
                    field["top_share"],
                    json.dumps([field["top_score"]] if field["top_score"] else []),
                    int(bool(market)),
                    odds["captured_at"] if odds else None,
                    json.dumps(market, sort_keys=True),
                    market_top,
                    market_share,
                    strategy_mode,
                    volatility,
                    readiness_status,
                    json.dumps(warnings, ensure_ascii=False),
                    input_fingerprint,
                    generated.isoformat(),
                    int(frozen_final),
                    "pre_deadline_final" if frozen_final else "field_refresh",
                    int(previous_row["id"]) if previous_row else None,
                ),
            )
            current_row = _record_row(conn, int(cursor.lastrowid))
        else:
            current_row = existing
        current = _recommendation_from_row(current_row)
        previous = _recommendation_from_row(previous_row) if previous_row else None
        if previous is None or (
            previous.recommended_score != current.recommended_score
            or previous.status != current.status
            or previous.frozen_final != current.frozen_final
        ):
            changed.append((previous, current))

    conn.commit()
    recommendations = _round_recommendations(conn, round_id)
    batch = ContestRecommendationBatch(
        season_id=season_id,
        round_id=round_id,
        round_name=resolved_round_name,
        recommendations=recommendations,
        changed=tuple(changed),
        field_complete_count=field_complete_count,
        field_expected_count=field_expected_count,
        readiness_status=(
            "blocked" if readiness["blocked_count"] else "attention" if readiness["attention_count"] else "ready"
        ),
        market_present_count=sum(item.market_present for item in recommendations),
        generated_at=generated.isoformat(),
        deadline_at=deadline_at,
        frozen_final=frozen_final,
        recomputed=True,
        deadline_locked=False,
    )
    if enqueue_update and batch.changed:
        if finalize:
            enqueue_contest_recommendation_notification(
                conn,
                batch=batch,
                kind="final",
                text=render_contest_recommendations(batch, final=True),
                chat_ids=notification_chat_ids,
            )
        elif all(previous is None for previous, _current in batch.changed):
            enqueue_contest_recommendation_notification(
                conn,
                batch=batch,
                kind="initial",
                text=render_contest_recommendations(batch),
                chat_ids=notification_chat_ids,
            )
        else:
            enqueue_contest_recommendation_notification(
                conn,
                batch=batch,
                kind="update",
                text=render_contest_recommendation_update(batch),
                chat_ids=notification_chat_ids,
            )
        conn.commit()
    return batch


def enqueue_contest_recommendation_notification(
    conn: sqlite3.Connection,
    *,
    batch: ContestRecommendationBatch,
    kind: str,
    text: str,
    chat_ids: Iterable[int],
) -> bool:
    event_key = ":".join(
        ("contest-recommendation", kind, str(batch.season_id), str(batch.round_id), batch.fingerprint)
    )
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO contest_recommendation_notifications(
            event_key, season_id, round_id, kind, batch_fingerprint, text, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (event_key, batch.season_id, batch.round_id, kind, batch.fingerprint, text[:MAX_TELEGRAM_TEXT], batch.generated_at),
    )
    if cursor.rowcount == 0:
        return False
    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    conn.executemany(
        """
        INSERT INTO contest_recommendation_notification_deliveries(event_key, chat_id, status, created_at)
        VALUES(?, ?, 'pending', ?)
        """,
        [(event_key, chat_id, batch.generated_at) for chat_id in recipients],
    )
    return True


def pending_contest_recommendation_deliveries(
    conn: sqlite3.Connection,
    chat_ids: Iterable[int],
) -> list[ContestRecommendationNotificationDelivery]:
    recipients = sorted({int(chat_id) for chat_id in chat_ids})
    if not recipients:
        return []
    placeholders = ", ".join("?" for _ in recipients)
    rows = conn.execute(
        f"""
        SELECT delivery.event_key, delivery.chat_id, event.text
        FROM contest_recommendation_notification_deliveries delivery
        JOIN contest_recommendation_notifications event ON event.event_key = delivery.event_key
        WHERE delivery.status = 'pending' AND delivery.chat_id IN ({placeholders})
        ORDER BY event.created_at, event.event_key, delivery.chat_id
        """,
        recipients,
    ).fetchall()
    return [
        ContestRecommendationNotificationDelivery(
            event_key=str(row["event_key"]), chat_id=int(row["chat_id"]), text=str(row["text"])
        )
        for row in rows
    ]


def mark_contest_recommendation_delivery_sent(
    conn: sqlite3.Connection,
    delivery: ContestRecommendationNotificationDelivery,
    *,
    sent_at: str,
) -> None:
    conn.execute(
        """
        UPDATE contest_recommendation_notification_deliveries
        SET status = 'sent', sent_at = ?, error = NULL, last_attempt_at = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (sent_at, sent_at, delivery.event_key, delivery.chat_id),
    )
    conn.commit()


def mark_contest_recommendation_delivery_failed(
    conn: sqlite3.Connection,
    delivery: ContestRecommendationNotificationDelivery,
    error: str,
    *,
    attempted_at: str,
) -> None:
    conn.execute(
        """
        UPDATE contest_recommendation_notification_deliveries
        SET attempts = attempts + 1, last_attempt_at = ?, error = ?
        WHERE event_key = ? AND chat_id = ? AND status = 'pending'
        """,
        (attempted_at, error[:500], delivery.event_key, delivery.chat_id),
    )
    conn.commit()


def _field_line(batch: ContestRecommendationBatch) -> str:
    missing = max(0, batch.field_expected_count - batch.field_complete_count)
    if missing:
        suffix = f"; {missing} участник ещё не прислал"
    else:
        suffix = ""
    return f"Поле: {batch.field_complete_count}/{batch.field_expected_count} прогнозов{suffix}."


def _batch_status(batch: ContestRecommendationBatch) -> str:
    statuses = {item.status for item in batch.recommendations}
    if "blocked" in statuses:
        return "blocked"
    if batch.frozen_final and statuses == {"final"}:
        return "final"
    if "provisional" in statuses:
        return "provisional"
    return batch.readiness_status


def render_contest_recommendations(batch: ContestRecommendationBatch, *, final: bool = False) -> str:
    status = _batch_status(batch)
    if final and status == "final":
        title = "🧠 Финальный прогноз Brucebet"
    elif final:
        title = "🧠 Прогноз Brucebet: снимок перед дедлайном"
    else:
        title = "🧠 Прогноз Brucebet"
    lines = [f"{title} — Тур {batch.round_name}"]
    for item in batch.recommendations:
        marker_parts = []
        if item.confidence is not None:
            marker_parts.append(f"{round(item.confidence * 100)}%")
        if item.field_top_outcome:
            marker_parts.append(f"поле {item.field_top_outcome}")
        if item.risk_level:
            marker_parts.append(f"риск {item.risk_level}")
        marker = f" ({', '.join(marker_parts)})" if marker_parts else ""
        lines.append(f"{item.position}. {item.home} — {item.away} — {item.recommended_score or 'нет оценки'}{marker}")
    lines.extend(
        [
            _field_line(batch),
            f"Модель: {batch.readiness_status}.",
            f"Рынок: {'актуален' if batch.market_present_count else 'нет данных'}.",
            f"Статус: {status}.",
        ]
    )
    return "\n".join(lines)[:MAX_TELEGRAM_TEXT]


def render_contest_recommendation_update(batch: ContestRecommendationBatch) -> str:
    status = _batch_status(batch)
    lines = [f"🧠 Brucebet пересчитал Тур {batch.round_name}"]
    for previous, current in batch.changed:
        old = previous.recommended_score if previous else "новый"
        lines.append(f"{current.home} — {current.away}: {old} → {current.recommended_score or 'нет оценки'}")
    lines.extend(
        [
            _field_line(batch),
            f"Модель: {batch.readiness_status}.",
            f"Рынок: {'актуален' if batch.market_present_count else 'нет данных'}.",
            f"Статус: {status}.",
        ]
    )
    return "\n".join(lines)[:MAX_TELEGRAM_TEXT]


def due_final_contest_rounds(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    lock_minutes: int,
    lead_minutes: int,
) -> tuple[str, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Final recommendation scheduler requires an aware timestamp")
    due: list[str] = []
    for item in round_deadlines(conn, lock_minutes=lock_minutes):
        deadline = item.effective_deadline_at
        if deadline is None:
            continue
        trigger = deadline - timedelta(minutes=max(1, lead_minutes))
        if trigger <= now < deadline:
            row = conn.execute(
                """
                SELECT 1
                FROM contest_recommendations recommendation
                JOIN rounds round ON round.id = recommendation.round_id
                WHERE round.name = ? AND recommendation.frozen_final = 1
                LIMIT 1
                """,
                (item.round_name,),
            ).fetchone()
            if row is None:
                due.append(item.round_name)
    return tuple(due)
