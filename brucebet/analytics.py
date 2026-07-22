from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import sqlite3

from .scoring import Score, is_prediction_eligible, normalize_score, parse_datetime, parse_score, score_prediction
from .storage import active_season, active_season_id


@dataclass
class ParticipantStats:
    participant_id: int
    name: str
    paid: bool
    total: int = 0
    exact_hits: int = 0
    diff_hits: int = 0
    outcome_hits: int = 0
    misses: int = 0
    invalid: int = 0
    late: int = 0
    pending: int = 0
    round_points: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    rank: int = 0
    prize_rub: int = 0


@dataclass(frozen=True)
class PredictionView:
    participant: str
    score: str
    valid: bool
    eligible: bool
    points: int
    category: str


@dataclass(frozen=True)
class RoundDeadline:
    round_name: str
    first_kickoff_at: datetime | None
    stored_deadline_at: datetime | None
    computed_deadline_at: datetime | None

    @property
    def effective_deadline_at(self) -> datetime | None:
        return self.computed_deadline_at or self.stored_deadline_at


@dataclass(frozen=True)
class CalendarItem:
    match_id: int
    round_name: str
    position: int
    label: str
    kickoff_at: datetime | None
    deadline_at: datetime | None
    status: str
    prediction_count: int
    my_prediction_count: int
    result: str | None


def _participants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    season_id = active_season_id(conn)
    return list(
        conn.execute(
            """
            SELECT
                p.id,
                p.name,
                COALESCE(sp.paid, p.paid) AS paid,
                COALESCE(sp.active, 1) AS active
            FROM participants p
            LEFT JOIN season_participants sp
                ON sp.participant_id = p.id AND sp.season_id = ?
            WHERE COALESCE(sp.active, 1) = 1
            ORDER BY p.name
            """,
            (season_id,),
        )
    )


def _scored_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    season_id = active_season_id(conn)
    return list(
        conn.execute(
            """
            SELECT
                p.name AS participant,
                p.id AS participant_id,
                COALESCE(sp.paid, p.paid) AS paid,
                r.id AS round_id,
                r.sort_order AS round_order,
                r.deadline_at AS round_deadline_at,
                m.id AS match_id,
                m.position,
                m.home,
                m.away,
                m.kickoff_at,
                m.result,
                pr.score,
                pr.submitted_at
            FROM predictions pr
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            JOIN participants p ON p.id = pr.participant_id
            LEFT JOIN season_participants sp
                ON sp.participant_id = p.id AND sp.season_id = r.season_id
            WHERE r.season_id = ?
              AND COALESCE(sp.active, 1) = 1
            ORDER BY r.sort_order, m.position, p.name
            """,
            (season_id,),
        )
    )


def prediction_is_eligible(
    submitted_at: datetime | None,
    kickoff_at: datetime | None,
    round_deadline_at: datetime | None,
    lock_minutes: int = 90,
) -> bool:
    if submitted_at is None:
        return True
    if round_deadline_at is not None and submitted_at <= round_deadline_at:
        return True
    if round_deadline_at is not None and kickoff_at is None:
        return False
    return is_prediction_eligible(submitted_at, kickoff_at, lock_minutes)


def compute_standings(
    conn: sqlite3.Connection,
    entry_fee_rub: int = 300,
    lock_minutes: int = 90,
) -> list[ParticipantStats]:
    stats = {
        int(row["id"]): ParticipantStats(
            participant_id=int(row["id"]),
            name=row["name"],
            paid=bool(row["paid"]),
        )
        for row in _participants(conn)
    }
    round_orders = [
        int(row["sort_order"])
        for row in conn.execute(
            "SELECT sort_order FROM rounds WHERE season_id = ? ORDER BY sort_order",
            (active_season_id(conn),),
        )
    ]

    for row in _scored_rows(conn):
        participant = stats[int(row["participant_id"])]
        prediction = parse_score(row["score"])
        result = parse_score(row["result"])
        submitted_at = parse_datetime(row["submitted_at"])
        kickoff_at = parse_datetime(row["kickoff_at"])
        round_deadline_at = parse_datetime(row["round_deadline_at"])

        if not prediction_is_eligible(submitted_at, kickoff_at, round_deadline_at, lock_minutes):
            participant.late += 1
            continue

        award = score_prediction(prediction, result)
        participant.total += award.points
        participant.round_points[int(row["round_order"])] += award.points
        if award.category == "exact":
            participant.exact_hits += 1
        elif award.category == "diff":
            participant.diff_hits += 1
        elif award.category == "outcome":
            participant.outcome_hits += 1
        elif award.category == "invalid":
            participant.invalid += 1
        elif award.category == "pending":
            participant.pending += 1
        elif award.category == "miss":
            participant.misses += 1

    def sort_key(item: ParticipantStats) -> tuple:
        late_rounds = tuple(-item.round_points.get(order, 0) for order in sorted(round_orders, reverse=True))
        return (-item.total, -item.exact_hits, -item.diff_hits, *late_rounds, item.name.lower())

    ordered = sorted(stats.values(), key=sort_key)
    for index, item in enumerate(ordered, start=1):
        item.rank = index

    bank = sum(1 for item in ordered if item.paid) * entry_fee_rub
    payouts = {1: 0.5, 2: 0.3, 3: 0.2}
    for item in ordered:
        if item.paid and item.rank in payouts:
            item.prize_rub = int(bank * payouts[item.rank])
    return ordered


def prediction_views_for_match(
    conn: sqlite3.Connection,
    match_id: int,
    scenario: Score | None = None,
    lock_minutes: int = 90,
) -> list[PredictionView]:
    match = conn.execute(
        """
        SELECT m.*, r.deadline_at AS round_deadline_at
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        WHERE m.id = ?
        """,
        (match_id,),
    ).fetchone()
    if match is None:
        raise ValueError(f"Unknown match id: {match_id}")

    result = scenario or parse_score(match["result"])
    rows = list(
        conn.execute(
            """
            SELECT p.name AS participant, pr.score, pr.submitted_at
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            WHERE pr.match_id = ?
            ORDER BY p.name
            """,
            (match_id,),
        )
    )
    views: list[PredictionView] = []
    kickoff_at = parse_datetime(match["kickoff_at"])
    round_deadline_at = parse_datetime(match["round_deadline_at"])
    for row in rows:
        prediction = parse_score(row["score"])
        submitted_at = parse_datetime(row["submitted_at"])
        eligible = prediction_is_eligible(submitted_at, kickoff_at, round_deadline_at, lock_minutes)
        if not eligible:
            views.append(PredictionView(row["participant"], row["score"], prediction is not None, False, 0, "late"))
            continue
        award = score_prediction(prediction, result)
        views.append(
            PredictionView(
                participant=row["participant"],
                score=row["score"],
                valid=prediction is not None,
                eligible=True,
                points=award.points,
                category=award.category,
            )
        )
    return views


def field_summary(conn: sqlite3.Connection, match_id: int) -> dict[str, Counter]:
    views = prediction_views_for_match(conn, match_id)
    scores: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    for view in views:
        score = parse_score(view.score)
        if score is None:
            scores["invalid"] += 1
            continue
        scores[score.label()] += 1
        if score.outcome > 0:
            outcomes["P1"] += 1
        elif score.outcome < 0:
            outcomes["P2"] += 1
        else:
            outcomes["X"] += 1
    return {"scores": scores, "outcomes": outcomes}


def round_deadlines(conn: sqlite3.Connection, lock_minutes: int = 90) -> list[RoundDeadline]:
    season_id = active_season_id(conn)
    rows = list(
        conn.execute(
            """
            SELECT
                r.name AS round_name,
                r.sort_order,
                r.deadline_at,
                MIN(m.kickoff_at) AS first_kickoff_at
            FROM rounds r
            LEFT JOIN matches m ON m.round_id = r.id
            WHERE r.season_id = ?
            GROUP BY r.id
            ORDER BY r.sort_order
            """,
            (season_id,),
        )
    )
    deadlines: list[RoundDeadline] = []
    for row in rows:
        first_kickoff_at = parse_datetime(row["first_kickoff_at"])
        stored_deadline_at = parse_datetime(row["deadline_at"])
        computed_deadline_at = (
            first_kickoff_at - timedelta(minutes=lock_minutes)
            if first_kickoff_at is not None
            else None
        )
        deadlines.append(
            RoundDeadline(
                round_name=row["round_name"],
                first_kickoff_at=first_kickoff_at,
                stored_deadline_at=stored_deadline_at,
                computed_deadline_at=computed_deadline_at,
            )
        )
    return deadlines


def _aware_for_compare(value: datetime | None, now: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value.astimezone(now.tzinfo)


def _match_deadline(
    kickoff_at: datetime | None,
    round_deadline_at: datetime | None,
    lock_minutes: int,
) -> datetime | None:
    if kickoff_at is not None:
        return kickoff_at - timedelta(minutes=lock_minutes)
    return round_deadline_at


def _calendar_status(
    kickoff_at: datetime | None,
    deadline_at: datetime | None,
    result: str | None,
    now: datetime,
) -> str:
    if result:
        return "played"
    comparable_deadline = _aware_for_compare(deadline_at, now)
    comparable_kickoff = _aware_for_compare(kickoff_at, now)
    if comparable_deadline is not None and comparable_deadline < now:
        return "locked"
    if comparable_deadline is not None and comparable_deadline <= now + timedelta(hours=6):
        return "deadline_soon"
    if comparable_kickoff is not None and comparable_kickoff.date() == now.date():
        return "today"
    return "scheduled"


def calendar_matches(
    conn: sqlite3.Connection,
    days: int = 7,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
    round_name: str | None = None,
    start_at: datetime | None = None,
    limit: int = 50,
    include_unknown_kickoff: bool = False,
) -> list[CalendarItem]:
    season_id = active_season_id(conn)
    now = start_at or datetime.now().astimezone()
    until = now + timedelta(days=days)
    params: list[object] = [user_participant, season_id]
    round_filter = ""
    if round_name:
        round_filter = "AND r.name = ?"
        params.append(round_name)
    rows = list(
        conn.execute(
            f"""
            SELECT
                m.id,
                m.position,
                m.home,
                m.away,
                m.kickoff_at,
                m.result,
                r.name AS round_name,
                r.sort_order,
                r.deadline_at AS round_deadline_at,
                COUNT(pr.id) AS prediction_count,
                SUM(CASE WHEN lower(p.name) = lower(?) THEN 1 ELSE 0 END) AS my_prediction_count
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            LEFT JOIN predictions pr ON pr.match_id = m.id
            LEFT JOIN participants p ON p.id = pr.participant_id
            WHERE r.season_id = ?
            {round_filter}
            GROUP BY m.id
            ORDER BY r.sort_order, m.position
            """,
            params,
        )
    )
    items: list[CalendarItem] = []
    for row in rows:
        kickoff_at = parse_datetime(row["kickoff_at"])
        comparable_kickoff = _aware_for_compare(kickoff_at, now)
        if comparable_kickoff is None:
            if not include_unknown_kickoff:
                continue
        elif comparable_kickoff < now or comparable_kickoff > until:
            continue
        round_deadline_at = parse_datetime(row["round_deadline_at"])
        deadline_at = _match_deadline(kickoff_at, round_deadline_at, lock_minutes)
        items.append(
            CalendarItem(
                match_id=int(row["id"]),
                round_name=row["round_name"],
                position=int(row["position"]),
                label=f"{row['home']} - {row['away']}",
                kickoff_at=kickoff_at,
                deadline_at=deadline_at,
                status=_calendar_status(kickoff_at, deadline_at, row["result"], now),
                prediction_count=int(row["prediction_count"]),
                my_prediction_count=int(row["my_prediction_count"] or 0),
                result=row["result"],
            )
        )
        if len(items) >= limit:
            break
    return items


def next_calendar_match(
    conn: sqlite3.Connection,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
) -> CalendarItem | None:
    matches = calendar_matches(
        conn,
        days=370,
        user_participant=user_participant,
        lock_minutes=lock_minutes,
        limit=50,
    )
    return next((item for item in matches if not item.result), None)


def target_round_name(conn: sqlite3.Connection, lock_minutes: int = 90) -> str | None:
    now = datetime.now().astimezone()
    deadlines = round_deadlines(conn, lock_minutes=lock_minutes)
    future = [
        item
        for item in deadlines
        if _aware_for_compare(item.effective_deadline_at, now) is not None
        and _aware_for_compare(item.effective_deadline_at, now) >= now
    ]
    if future:
        return future[0].round_name
    if deadlines:
        return deadlines[-1].round_name
    return None


def match_rows_for_round(conn: sqlite3.Connection, round_name: str | None = None) -> list[sqlite3.Row]:
    season_id = active_season_id(conn)
    if round_name is None:
        round_name = target_round_name(conn)
    if round_name is None:
        return []
    return list(
        conn.execute(
            """
            SELECT m.*, r.name AS round_name, r.sort_order, r.deadline_at
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND r.name = ?
            ORDER BY m.position
            """,
            (season_id, round_name),
        )
    )


def risk_map(conn: sqlite3.Connection, round_name: str | None = None) -> dict[str, object]:
    matches = match_rows_for_round(conn, round_name)
    if not matches:
        return {"round_name": round_name, "safe": [], "slippery": [], "risk": [], "unknown": []}

    categories: dict[str, list[dict[str, object]]] = {"safe": [], "slippery": [], "risk": [], "unknown": []}
    for match in matches:
        summary = field_summary(conn, int(match["id"]))
        outcomes = summary["outcomes"]
        total = sum(outcomes.values())
        top = outcomes.most_common(1)[0] if outcomes else ("", 0)
        top_share = top[1] / total if total else 0.0
        assessment = conn.execute(
            "SELECT risk_level, suggested_score, contrarian_note FROM match_assessments WHERE match_id = ?",
            (int(match["id"]),),
        ).fetchone()
        risk_level = assessment["risk_level"] if assessment and assessment["risk_level"] else None
        if risk_level in {"low", "safe"}:
            bucket = "safe"
        elif risk_level in {"high", "risk"}:
            bucket = "risk"
        elif risk_level == "medium":
            bucket = "slippery"
        elif total == 0:
            bucket = "unknown"
        elif top_share >= 0.75:
            bucket = "safe"
        elif top_share >= 0.55:
            bucket = "slippery"
        else:
            bucket = "risk"

        categories[bucket].append(
            {
                "match_id": int(match["id"]),
                "round_name": match["round_name"],
                "position": int(match["position"]),
                "label": f"{match['home']} - {match['away']}",
                "top_outcome": top[0],
                "top_share": round(top_share, 2),
                "predictions": total,
                "suggested_score": assessment["suggested_score"] if assessment else "",
                "contrarian_note": assessment["contrarian_note"] if assessment else "",
            }
        )
    return {"round_name": matches[0]["round_name"], **categories}


def _outcome_for_score(raw: str | None) -> str | None:
    score = parse_score(raw)
    if score is None:
        return None
    return "P1" if score.outcome > 0 else "P2" if score.outcome < 0 else "X"


def _top_probability(values: dict[str, float]) -> tuple[str | None, float | None]:
    if not values:
        return None, None
    label, probability = max(values.items(), key=lambda item: item[1])
    return label, round(probability, 2)


def _normalised_market_probabilities(row: sqlite3.Row | None) -> dict[str, float]:
    if row is None:
        return {}
    raw = {"P1": row["home_win"], "X": row["draw"], "P2": row["away_win"]}
    implied: dict[str, float] = {}
    for label, odds in raw.items():
        try:
            value = float(odds)
        except (TypeError, ValueError):
            continue
        if value > 0:
            implied[label] = 1 / value
    total = sum(implied.values())
    if total == 0 or len(implied) != 3:
        return {}
    return {label: value / total for label, value in implied.items()}


def edge_map(conn: sqlite3.Connection, round_name: str | None = None) -> dict[str, object]:
    """Rank contest matches where field, market, and model disagree.

    This is a contest-strategy map, not a claim that one of the three sources is
    objectively right. Missing signals are reported separately instead of being
    treated as a contrarian opportunity.
    """
    matches = match_rows_for_round(conn, round_name)
    if not matches:
        return {"round_name": round_name, "opportunities": [], "needs_data": []}

    opportunities: list[dict[str, object]] = []
    needs_data: list[dict[str, object]] = []
    for match in matches:
        match_id = int(match["id"])
        field = field_summary(conn, match_id)["outcomes"]
        field_total = sum(field.values())
        field_outcome, field_share = (None, None)
        if field_total:
            top = field.most_common(1)[0]
            field_outcome, field_share = top[0], round(top[1] / field_total, 2)

        assessment = conn.execute("SELECT * FROM match_assessments WHERE match_id = ?", (match_id,)).fetchone()
        model_score = assessment["suggested_score"] if assessment else None
        model_outcome = _outcome_for_score(model_score)
        model_probabilities: dict[str, float] = {}
        if assessment:
            for label, key in (("P1", "home_edge"), ("X", "draw_edge"), ("P2", "away_edge")):
                try:
                    value = float(assessment[key])
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    model_probabilities[label] = value
        model_top, model_share = _top_probability(model_probabilities)
        model_outcome = model_outcome or model_top

        odds = conn.execute(
            "SELECT * FROM match_odds WHERE match_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        market_probabilities = _normalised_market_probabilities(odds)
        market_outcome, market_share = _top_probability(market_probabilities)

        missing: list[str] = []
        if field_outcome is None:
            missing.append("field")
        if model_outcome is None:
            missing.append("model")
        if market_outcome is None:
            missing.append("market")

        row: dict[str, object] = {
            "match_id": match_id,
            "position": int(match["position"]),
            "label": f"{match['home']} - {match['away']}",
            "model_score": model_score or "",
            "model_outcome": model_outcome or "",
            "model_share": model_share,
            "market_outcome": market_outcome or "",
            "market_share": market_share,
            "field_outcome": field_outcome or "",
            "field_share": field_share,
            "field_predictions": field_total,
            "volatility": assessment["volatility"] if assessment else None,
            "missing": missing,
            "signals": [],
            "edge_score": None,
            "note": assessment["contrarian_note"] if assessment else "",
        }
        if missing:
            needs_data.append(row)
            continue

        signals: list[str] = []
        if model_outcome != field_outcome:
            signals.append("model-field")
        if model_outcome != market_outcome:
            signals.append("model-market")
        if field_outcome != market_outcome:
            signals.append("field-market")
        try:
            volatility = max(0.0, min(1.0, float(assessment["volatility"]))) if assessment else 0.0
        except (TypeError, ValueError):
            volatility = 0.0
        model_market_gap = abs(model_probabilities.get(model_outcome, 0.0) - market_probabilities.get(model_outcome, 0.0))
        edge_score = (
            0.35 * (len(signals) / 3)
            + 0.30 * (1 - float(field_share))
            + 0.20 * volatility
            + 0.15 * model_market_gap
        )
        row["signals"] = signals
        row["edge_score"] = round(edge_score, 2)
        opportunities.append(row)

    opportunities.sort(key=lambda item: (-float(item["edge_score"]), int(item["position"])))
    needs_data.sort(key=lambda item: int(item["position"]))
    return {"round_name": matches[0]["round_name"], "opportunities": opportunities, "needs_data": needs_data}


def _freshness_item(raw: str | None, now: datetime) -> dict[str, object]:
    updated_at = parse_datetime(raw)
    comparable = _aware_for_compare(updated_at, now)
    age_minutes = None
    if comparable is not None:
        age_minutes = max(0, int((now - comparable).total_seconds() // 60))
    return {"updated_at": raw, "age_minutes": age_minutes}


def data_freshness(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[str, object]]:
    """Return the latest timestamp for every signal used in match decisions."""
    now = now or datetime.now().astimezone()
    season_id = active_season_id(conn)
    queries = {
        "fpl": "SELECT MAX(updated_at) AS updated_at FROM player_status_snapshots WHERE source = 'FPL'",
        "elo": "SELECT MAX(updated_at) AS updated_at FROM teams WHERE elo_rating IS NOT NULL",
        "odds": """
            SELECT MAX(mo.captured_at) AS updated_at
            FROM match_odds mo
            JOIN matches m ON m.id = mo.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
        """,
        "model": """
            SELECT MAX(ma.updated_at) AS updated_at
            FROM match_assessments ma
            JOIN matches m ON m.id = ma.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ?
        """,
        "results": "SELECT MAX(finished_at) AS updated_at FROM result_sync_runs",
    }
    values: dict[str, str | None] = {}
    for key, query in queries.items():
        params: tuple[object, ...] = (season_id,) if key in {"odds", "model"} else ()
        row = conn.execute(query, params).fetchone()
        values[key] = row["updated_at"] if row else None
    return {key: _freshness_item(value, now) for key, value in values.items()}


def hq_summary(
    conn: sqlite3.Connection,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
) -> dict[str, object]:
    season = active_season(conn)
    round_name = target_round_name(conn, lock_minutes=lock_minutes)
    matches = match_rows_for_round(conn, round_name)
    deadlines = {item.round_name: item for item in round_deadlines(conn, lock_minutes=lock_minutes)}
    deadline = deadlines.get(round_name) if round_name else None
    participants = _participants(conn)
    paid_count = sum(1 for row in participants if bool(row["paid"]))
    season_id = active_season_id(conn)

    prediction_counts = {"participants": 0, "rows": 0, "mine": 0}
    if round_name:
        row = conn.execute(
            """
            SELECT COUNT(*) AS rows_count, COUNT(DISTINCT pr.participant_id) AS participants_count
            FROM predictions pr
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND r.name = ?
            """,
            (season_id, round_name),
        ).fetchone()
        mine = conn.execute(
            """
            SELECT COUNT(*) AS rows_count
            FROM predictions pr
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            WHERE r.season_id = ? AND r.name = ? AND lower(p.name) = lower(?)
            """,
            (season_id, round_name, user_participant),
        ).fetchone()
        prediction_counts = {
            "participants": int(row["participants_count"]),
            "rows": int(row["rows_count"]),
            "mine": int(mine["rows_count"]),
        }

    risk = risk_map(conn, round_name)
    return {
        "season": season,
        "round_name": round_name,
        "deadline": deadline,
        "match_count": len(matches),
        "participant_count": len(participants),
        "paid_count": paid_count,
        "bank_rub": paid_count * int(season["entry_fee_rub"]),
        "predictions": prediction_counts,
        "risk": risk,
        "freshness": data_freshness(conn),
    }


def ready_summary(
    conn: sqlite3.Connection,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
    now: datetime | None = None,
) -> dict[str, object]:
    """Preflight the active round before committing a prediction to the contest."""
    now = now or datetime.now().astimezone()
    hq = hq_summary(conn, user_participant=user_participant, lock_minutes=lock_minutes)
    round_name = hq["round_name"]
    matches = match_rows_for_round(conn, round_name)
    deadline = hq["deadline"]
    effective_deadline = deadline.effective_deadline_at if deadline else None
    comparable_deadline = _aware_for_compare(effective_deadline, now)
    minutes_to_deadline = (
        int((comparable_deadline - now).total_seconds() // 60)
        if comparable_deadline is not None
        else None
    )
    unknown_kickoff = [int(row["position"]) for row in matches if parse_datetime(row["kickoff_at"]) is None]
    participants = _participants(conn)
    user_present = any(str(row["name"]).lower() == user_participant.lower() for row in participants)
    missing_mine = max(0, len(matches) - int(hq["predictions"]["mine"]))
    expected_field_rows = len(matches) * len(participants)
    missing_field = max(0, expected_field_rows - int(hq["predictions"]["rows"]))
    model_coverage = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM model_forecasts f
        JOIN matches m ON m.id = f.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ? AND r.name = ?
        """,
        (active_season_id(conn), round_name),
    ).fetchone()
    blockers: list[str] = []
    warnings: list[str] = []
    if not matches:
        blockers.append("Нет матчей активного тура.")
    if effective_deadline is None:
        blockers.append("Не вычислен дедлайн тура.")
    if unknown_kickoff:
        blockers.append(f"Нет kickoff у матчей: {', '.join(map(str, unknown_kickoff))}.")
    if minutes_to_deadline is not None and minutes_to_deadline < 0:
        blockers.append("Дедлайн уже прошёл.")
    if participants and not user_present:
        blockers.append(f"Участник {user_participant} не загружен в активный сезон.")
    elif user_present and missing_mine:
        warnings.append(f"Не хватает твоих прогнозов: {missing_mine}.")
    if not participants:
        warnings.append("Участники пока не загружены.")
    elif missing_field:
        warnings.append(f"Поле ещё неполное: не хватает {missing_field} строк прогнозов.")
    if len(matches) and int(model_coverage["count"]) < len(matches):
        warnings.append(f"Модель зафиксирована не для всех матчей: {model_coverage['count']}/{len(matches)}.")

    freshness = hq["freshness"]
    nearing_deadline = minutes_to_deadline is not None and minutes_to_deadline <= 72 * 60
    for key, title, threshold in [("fpl", "FPL", 36 * 60), ("model", "модель", 48 * 60)]:
        item = freshness[key]
        if item["updated_at"] is None:
            warnings.append(f"Нет данных источника: {title}.")
        elif item["age_minutes"] is not None and item["age_minutes"] > threshold:
            warnings.append(f"Данные {title} устарели: {item['age_minutes']} мин.")
    if nearing_deadline:
        odds = freshness["odds"]
        if odds["updated_at"] is None:
            warnings.append("Перед близким дедлайном нет сохранённых кэфов.")
        elif odds["age_minutes"] is not None and odds["age_minutes"] > 36 * 60:
            warnings.append(f"Кэфы устарели: {odds['age_minutes']} мин.")

    status = "blocked" if blockers else "attention" if warnings else "ready"
    return {
        "status": status,
        "round_name": round_name,
        "deadline": effective_deadline,
        "minutes_to_deadline": minutes_to_deadline,
        "match_count": len(matches),
        "unknown_kickoff": unknown_kickoff,
        "participants": len(participants),
        "your_predictions": int(hq["predictions"]["mine"]),
        "missing_your_predictions": missing_mine if user_present else None,
        "field_predictions": int(hq["predictions"]["rows"]),
        "expected_field_predictions": expected_field_rows,
        "model_forecasts": int(model_coverage["count"]),
        "freshness": freshness,
        "blockers": blockers,
        "warnings": warnings,
    }


def missing_forecasts_summary(
    conn: sqlite3.Connection,
    round_name: str | None = None,
    lock_minutes: int = 90,
) -> dict[str, object]:
    """Return an operator-friendly per-person coverage view for one round."""
    selected_round = round_name.strip() if round_name else target_round_name(conn, lock_minutes=lock_minutes)
    if not selected_round:
        return {
            "round_name": None,
            "deadline": None,
            "match_count": 0,
            "participant_count": 0,
            "complete_count": 0,
            "incomplete": [],
        }

    season_id = active_season_id(conn)
    matches = match_rows_for_round(conn, selected_round)
    if not matches:
        raise ValueError(f"No matches found for round {selected_round!r}")
    deadline_by_round = {item.round_name: item for item in round_deadlines(conn, lock_minutes=lock_minutes)}
    participants = _participants(conn)
    rows = list(
        conn.execute(
            """
            SELECT
                p.name AS participant,
                COUNT(pr.id) AS submitted_count,
                GROUP_CONCAT(CASE WHEN pr.id IS NULL THEN m.position END, ',') AS missing_positions
            FROM season_participants sp
            JOIN participants p ON p.id = sp.participant_id
            CROSS JOIN matches m
            JOIN rounds r ON r.id = m.round_id
            LEFT JOIN predictions pr ON pr.participant_id = p.id AND pr.match_id = m.id
            WHERE sp.season_id = ?
              AND sp.active = 1
              AND r.season_id = ?
              AND r.name = ?
            GROUP BY p.id
            ORDER BY submitted_count ASC, p.name
            """,
            (season_id, season_id, selected_round),
        )
    )
    incomplete = [
        {
            "participant": row["participant"],
            "submitted_count": int(row["submitted_count"]),
            "missing_positions": tuple(int(value) for value in (row["missing_positions"] or "").split(",") if value),
        }
        for row in rows
        if int(row["submitted_count"]) < len(matches)
    ]
    deadline = deadline_by_round.get(selected_round)
    return {
        "round_name": selected_round,
        "deadline": deadline.effective_deadline_at if deadline else None,
        "match_count": len(matches),
        "participant_count": len(participants),
        "complete_count": len(participants) - len(incomplete),
        "incomplete": incomplete,
    }


def strategy_summary(
    conn: sqlite3.Connection,
    user_participant: str = "Bruce Wayne",
    lock_minutes: int = 90,
) -> dict[str, object]:
    standings = compute_standings(conn, lock_minutes=lock_minutes)
    me = next((item for item in standings if item.name.lower() == user_participant.lower()), None)
    leader = standings[0] if standings else None
    gap = (leader.total - me.total) if leader and me else None
    if me is None or leader is None:
        mode = "unknown"
        advice = "Нужны участники и хотя бы часть прогнозов/результатов, чтобы строить стратегию."
    elif me.rank == 1:
        mode = "protect"
        advice = "Ты впереди. Играй базу, отличайся точечно: 1-2 матча, где поле реально переоценивает фаворита."
    elif gap is not None and gap <= 3:
        mode = "balanced"
        advice = "Отставание небольшое. Не надо ломать тур: ищи 1-2 аккуратных отличия от поля."
    elif gap is not None and gap <= 8:
        mode = "chase"
        advice = "Нужно догонять, но не широким фронтом. Цель: 2-3 отличия, в основном в риск-матчах."
    else:
        mode = "aggressive"
        advice = "Нужен апсайд. Ищи 3-4 отличия, но избегай бессмысленных 4:0 и случайных побед аутсайдера."
    return {
        "user": user_participant,
        "me": me,
        "leader": leader,
        "gap": gap,
        "mode": mode,
        "advice": advice,
        "risk": risk_map(conn),
    }


def capture_model_forecasts(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    model_key: str = "brucebet",
) -> int:
    """Freeze the current model draft before kickoff for later honest calibration."""
    now = now or datetime.now().astimezone()
    season_id = active_season_id(conn)
    rows = conn.execute(
        """
        SELECT
            m.id AS match_id,
            m.kickoff_at,
            a.suggested_score,
            a.confidence,
            a.risk_level,
            a.updated_at
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        JOIN match_assessments a ON a.match_id = m.id
        WHERE r.season_id = ?
        ORDER BY r.sort_order, m.position
        """,
        (season_id,),
    )
    captured = 0
    for row in rows:
        score = normalize_score(row["suggested_score"])
        kickoff = parse_datetime(row["kickoff_at"])
        if score is None or (kickoff is not None and kickoff <= now):
            continue
        cursor = conn.execute(
            """
            INSERT INTO model_forecasts(
                match_id, model_key, suggested_score, confidence, risk_level, captured_at, assessment_updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, model_key) DO NOTHING
            """,
            (
                int(row["match_id"]),
                model_key,
                score,
                row["confidence"],
                row["risk_level"],
                now.isoformat(),
                row["updated_at"],
            ),
        )
        captured += int(cursor.rowcount)
    conn.commit()
    return captured


def model_calibration_summary(
    conn: sqlite3.Connection,
    round_name: str | None = None,
    model_key: str = "brucebet",
) -> dict[str, object]:
    season_id = active_season_id(conn)
    where_round = "AND r.name = ?" if round_name else ""
    params: tuple[object, ...] = (season_id, model_key, round_name) if round_name else (season_id, model_key)
    rows = conn.execute(
        f"""
        SELECT
            r.name AS round_name,
            r.sort_order,
            m.id AS match_id,
            m.home,
            m.away,
            m.result,
            f.suggested_score,
            f.confidence,
            f.risk_level,
            f.captured_at
        FROM model_forecasts f
        JOIN matches m ON m.id = f.match_id
        JOIN rounds r ON r.id = m.round_id
        WHERE r.season_id = ? AND f.model_key = ? {where_round}
        ORDER BY r.sort_order, m.position
        """,
        params,
    ).fetchall()
    totals: Counter[str] = Counter()
    buckets: dict[str, Counter[str]] = {"low": Counter(), "medium": Counter(), "high": Counter()}
    matches: list[dict[str, object]] = []
    for row in rows:
        award = score_prediction(parse_score(row["suggested_score"]), parse_score(row["result"]))
        bucket = "low"
        confidence = float(row["confidence"] or 0)
        if confidence >= 0.65:
            bucket = "high"
        elif confidence >= 0.45:
            bucket = "medium"
        totals[award.category] += 1
        totals["points"] += award.points
        buckets[bucket][award.category] += 1
        buckets[bucket]["points"] += award.points
        matches.append(
            {
                "round_name": row["round_name"],
                "match": f"{row['home']} - {row['away']}",
                "forecast": row["suggested_score"],
                "result": row["result"] or "",
                "category": award.category,
                "points": award.points,
                "confidence": row["confidence"],
                "risk_level": row["risk_level"] or "",
            }
        )
    scored = sum(totals[key] for key in ("exact", "diff", "outcome", "miss"))
    return {
        "model_key": model_key,
        "forecasts": len(rows),
        "scored": scored,
        "pending": totals["pending"],
        "exact": totals["exact"],
        "diff": totals["diff"],
        "outcome": totals["outcome"],
        "miss": totals["miss"],
        "points": totals["points"],
        "points_per_match": round(totals["points"] / scored, 2) if scored else 0.0,
        "buckets": {
            key: {
                "forecasts": sum(value[item] for item in ("exact", "diff", "outcome", "miss", "pending")),
                "points": value["points"],
                "exact": value["exact"],
                "diff": value["diff"],
                "outcome": value["outcome"],
                "miss": value["miss"],
            }
            for key, value in buckets.items()
        },
        "matches": matches,
    }


def round_review(conn: sqlite3.Connection, round_name: str, lock_minutes: int = 90) -> dict[str, object]:
    """Build a compact factual debrief for one round, including score swings."""
    season_id = active_season_id(conn)
    round_row = conn.execute(
        "SELECT id, name, sort_order, deadline_at FROM rounds WHERE season_id = ? AND name = ?",
        (season_id, round_name),
    ).fetchone()
    if round_row is None:
        raise ValueError(f"Unknown round: {round_name}")
    matches = list(
        conn.execute(
            "SELECT id, position, home, away, kickoff_at, result FROM matches WHERE round_id = ? ORDER BY position",
            (int(round_row["id"]),),
        )
    )
    participants = {
        int(row["id"]): {
            "participant": row["name"],
            "points": 0,
            "exact": 0,
            "diff": 0,
            "outcome": 0,
            "miss": 0,
            "late": 0,
        }
        for row in _participants(conn)
    }
    predictions = conn.execute(
        """
        SELECT pr.participant_id, pr.match_id, pr.score, pr.submitted_at
        FROM predictions pr
        JOIN matches m ON m.id = pr.match_id
        WHERE m.round_id = ?
        """,
        (int(round_row["id"]),),
    ).fetchall()
    match_by_id = {int(match["id"]): match for match in matches}
    points_by_match: dict[int, list[int]] = defaultdict(list)
    deadline_at = parse_datetime(round_row["deadline_at"])
    for row in predictions:
        participant = participants.get(int(row["participant_id"]))
        match = match_by_id.get(int(row["match_id"]))
        if participant is None or match is None:
            continue
        eligible = prediction_is_eligible(
            parse_datetime(row["submitted_at"]),
            parse_datetime(match["kickoff_at"]),
            deadline_at,
            lock_minutes,
        )
        if not eligible:
            participant["late"] += 1
            continue
        award = score_prediction(parse_score(row["score"]), parse_score(match["result"]))
        participant["points"] += award.points
        participant[award.category] = int(participant.get(award.category, 0)) + 1
        if parse_score(match["result"]):
            points_by_match[int(match["id"])].append(award.points)
    participant_rows = sorted(
        participants.values(),
        key=lambda row: (-int(row["points"]), -int(row["exact"]), -int(row["diff"]), str(row["participant"]).lower()),
    )
    swings = []
    for match in matches:
        points = points_by_match.get(int(match["id"]), [])
        if not points or parse_score(match["result"]) is None:
            continue
        swings.append(
            {
                "position": int(match["position"]),
                "match": f"{match['home']} - {match['away']}",
                "result": match["result"],
                "spread": max(points) - min(points),
                "max_points": max(points),
                "min_points": min(points),
            }
        )
    swings.sort(key=lambda row: (-int(row["spread"]), int(row["position"])))
    finished = sum(1 for match in matches if parse_score(match["result"]) is not None)
    return {
        "round_name": round_row["name"],
        "round_id": int(round_row["id"]),
        "match_count": len(matches),
        "finished_count": finished,
        "complete": bool(matches) and finished == len(matches),
        "participants": participant_rows,
        "swings": swings[:5],
        "calibration": model_calibration_summary(conn, round_name=round_name),
    }


def finalize_completed_rounds(conn: sqlite3.Connection, lock_minutes: int = 90) -> list[dict[str, object]]:
    """Persist a review whenever every match in a round has a valid final score."""
    season_id = active_season_id(conn)
    rounds = conn.execute(
        "SELECT id, name FROM rounds WHERE season_id = ? ORDER BY sort_order",
        (season_id,),
    ).fetchall()
    saved: list[dict[str, object]] = []
    completed_at = datetime.now().astimezone().isoformat()
    for row in rounds:
        review = round_review(conn, str(row["name"]), lock_minutes=lock_minutes)
        if not review["complete"]:
            continue
        conn.execute(
            """
            INSERT INTO round_reviews(round_id, completed_at, match_count, finished_count, payload_json)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(round_id) DO UPDATE SET
                completed_at = excluded.completed_at,
                match_count = excluded.match_count,
                finished_count = excluded.finished_count,
                payload_json = excluded.payload_json
            """,
            (
                int(row["id"]),
                completed_at,
                int(review["match_count"]),
                int(review["finished_count"]),
                json.dumps(review, ensure_ascii=True, sort_keys=True),
            ),
        )
        saved.append(review)
    conn.commit()
    return saved


def recommend_match(conn: sqlite3.Connection, match_id: int) -> dict[str, object]:
    dossier = match_dossier(conn, match_id)
    summary = field_summary(conn, match_id)
    scores = summary["scores"]
    outcomes = summary["outcomes"]
    valid_scores = Counter({key: value for key, value in scores.items() if key != "invalid"})
    top_score = valid_scores.most_common(1)[0][0] if valid_scores else ""
    top_outcome = outcomes.most_common(1)[0] if outcomes else ("", 0)
    total_outcomes = sum(outcomes.values())
    top_outcome_share = top_outcome[1] / total_outcomes if total_outcomes else 0

    assessment = dossier["assessment"]
    suggested_score = assessment["suggested_score"] if assessment and assessment["suggested_score"] else top_score
    risk_level = assessment["risk_level"] if assessment and assessment["risk_level"] else None
    confidence = assessment["confidence"] if assessment and assessment["confidence"] is not None else round(top_outcome_share, 2)
    contrarian_note = assessment["contrarian_note"] if assessment and assessment["contrarian_note"] else ""
    consensus_note = assessment["consensus_note"] if assessment and assessment["consensus_note"] else ""

    if risk_level is None:
        if total_outcomes == 0:
            risk_level = "unknown"
        elif top_outcome_share >= 0.75:
            risk_level = "low"
        elif top_outcome_share >= 0.55:
            risk_level = "medium"
        else:
            risk_level = "high"

    return {
        "match": dossier["match"],
        "suggested_score": suggested_score,
        "risk_level": risk_level,
        "confidence": confidence,
        "outcomes": outcomes,
        "scores": scores,
        "top_outcome_share": round(top_outcome_share, 2),
        "consensus_note": consensus_note,
        "contrarian_note": contrarian_note,
        "assessment": assessment,
    }


def compare_participants(
    conn: sqlite3.Connection,
    me: str,
    opponent: str,
    lock_minutes: int = 90,
) -> list[dict[str, object]]:
    season_id = active_season_id(conn)
    rows = list(
        conn.execute(
            """
            SELECT
                r.name AS round_name,
                r.sort_order,
                r.deadline_at AS round_deadline_at,
                m.id AS match_id,
                m.position,
                m.home,
                m.away,
                m.kickoff_at,
                m.result,
                mine.score AS my_score,
                mine.submitted_at AS my_submitted_at,
                opp.score AS opponent_score,
                opp.submitted_at AS opponent_submitted_at
            FROM matches m
            JOIN rounds r ON r.id = m.round_id
            JOIN predictions mine ON mine.match_id = m.id
            JOIN participants me ON me.id = mine.participant_id
            JOIN predictions opp ON opp.match_id = m.id
            JOIN participants opponent ON opponent.id = opp.participant_id
            WHERE r.season_id = ?
              AND lower(me.name) = lower(?)
              AND lower(opponent.name) = lower(?)
            ORDER BY r.sort_order, m.position
            """,
            (season_id, me, opponent),
        )
    )
    comparison: list[dict[str, object]] = []
    for row in rows:
        my_prediction = parse_score(row["my_score"])
        opponent_prediction = parse_score(row["opponent_score"])
        if my_prediction == opponent_prediction:
            continue

        kickoff_at = parse_datetime(row["kickoff_at"])
        round_deadline_at = parse_datetime(row["round_deadline_at"])
        result = parse_score(row["result"])
        my_eligible = prediction_is_eligible(
            parse_datetime(row["my_submitted_at"]),
            kickoff_at,
            round_deadline_at,
            lock_minutes,
        )
        opp_eligible = prediction_is_eligible(
            parse_datetime(row["opponent_submitted_at"]),
            kickoff_at,
            round_deadline_at,
            lock_minutes,
        )
        my_award = score_prediction(my_prediction if my_eligible else None, result)
        opp_award = score_prediction(opponent_prediction if opp_eligible else None, result)
        comparison.append(
            {
                "round": row["round_name"],
                "position": int(row["position"]),
                "match": f"{row['home']} - {row['away']}",
                "result": row["result"] or "",
                "mine": row["my_score"],
                "opponent": row["opponent_score"],
                "delta": my_award.points - opp_award.points if result else None,
            }
        )
    return comparison


def match_header(match: sqlite3.Row) -> str:
    result = f", result {match['result']}" if match["result"] else ""
    return f"Round {match['round_name']}, #{match['position']}: {match['home']} - {match['away']}{result}"


def find_team(conn: sqlite3.Connection, query: str) -> sqlite3.Row:
    value = query.strip()
    row = conn.execute(
        """
        SELECT * FROM teams
        WHERE lower(name) = lower(?)
           OR lower(COALESCE(short_name, '')) = lower(?)
        LIMIT 1
        """,
        (value, value),
    ).fetchone()
    if row:
        return row
    like = f"%{value}%"
    row = conn.execute(
        """
        SELECT * FROM teams
        WHERE name LIKE ? OR COALESCE(short_name, '') LIKE ?
        ORDER BY name
        LIMIT 1
        """,
        (like, like),
    ).fetchone()
    if row is None:
        raise ValueError(f"Team not found: {query}")
    return row


def team_profile(conn: sqlite3.Connection, query: str, form_limit: int = 5) -> dict[str, object]:
    team = find_team(conn, query)
    form = list(
        conn.execute(
            """
            SELECT * FROM team_form
            WHERE team_id = ?
            ORDER BY match_date DESC
            LIMIT ?
            """,
            (int(team["id"]), form_limit),
        )
    )
    absences = list(
        conn.execute(
            """
            SELECT * FROM absences
            WHERE team_id = ?
            ORDER BY impact_rating DESC NULLS LAST, player
            """,
            (int(team["id"]),),
        )
    )
    return {"team": team, "form": form, "absences": absences}


def player_status_summary(
    conn: sqlite3.Connection,
    team_query: str | None = None,
    limit: int = 30,
) -> list[sqlite3.Row]:
    params: list[object] = []
    team_filter = ""
    if team_query:
        team = find_team(conn, team_query)
        team_filter = "AND ps.team_id = ?"
        params.append(int(team["id"]))
    params.append(limit)
    return list(
        conn.execute(
            f"""
            SELECT ps.*, t.name AS team
            FROM player_status_snapshots ps
            JOIN teams t ON t.id = ps.team_id
            JOIN (
                SELECT team_id, player, MAX(updated_at) AS updated_at
                FROM player_status_snapshots
                GROUP BY team_id, player
            ) latest
              ON latest.team_id = ps.team_id
             AND latest.player = ps.player
             AND latest.updated_at = ps.updated_at
            WHERE 1 = 1
            {team_filter}
            ORDER BY
                CASE
                    WHEN ps.status IN ('out', 'injured', 'suspended') THEN 0
                    WHEN ps.status IN ('doubtful', 'questionable') THEN 1
                    ELSE 2
                END,
                ps.availability_pct ASC NULLS LAST,
                ps.form_rating DESC NULLS LAST,
                t.name,
                ps.player
            LIMIT ?
            """,
            params,
        )
    )


def match_dossier(conn: sqlite3.Connection, match_id: int) -> dict[str, object]:
    match = conn.execute(
        """
        SELECT m.*, r.name AS round_name, r.sort_order
        FROM matches m
        JOIN rounds r ON r.id = m.round_id
        WHERE m.id = ?
        """,
        (match_id,),
    ).fetchone()
    if match is None:
        raise ValueError(f"Unknown match id: {match_id}")

    home = find_team(conn, match["home"])
    away = find_team(conn, match["away"])
    context = conn.execute("SELECT * FROM match_contexts WHERE match_id = ?", (match_id,)).fetchone()
    assessment = conn.execute("SELECT * FROM match_assessments WHERE match_id = ?", (match_id,)).fetchone()
    odds = list(
        conn.execute(
            """
            SELECT * FROM match_odds
            WHERE match_id = ?
            ORDER BY captured_at DESC, bookmaker
            """,
            (match_id,),
        )
    )
    factors = list(
        conn.execute(
            """
            SELECT f.*, t.name AS team
            FROM team_match_factors f
            JOIN teams t ON t.id = f.team_id
            WHERE f.match_id = ?
            ORDER BY f.side
            """,
            (match_id,),
        )
    )
    absences = list(
        conn.execute(
            """
            SELECT t.name AS team, a.*
            FROM absences a
            JOIN teams t ON t.id = a.team_id
            WHERE t.id IN (?, ?)
            ORDER BY t.name, a.impact_rating DESC NULLS LAST, a.player
            """,
            (int(home["id"]), int(away["id"])),
        )
    )
    return {
        "match": match,
        "home": home,
        "away": away,
        "context": context,
        "assessment": assessment,
        "odds": odds,
        "factors": factors,
        "absences": absences,
    }
