from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import sqlite3

from .scoring import Score, is_prediction_eligible, parse_datetime, parse_score, score_prediction


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


def _participants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM participants ORDER BY name"))


def _scored_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                p.name AS participant,
                p.id AS participant_id,
                p.paid,
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
            JOIN participants p ON p.id = pr.participant_id
            JOIN matches m ON m.id = pr.match_id
            JOIN rounds r ON r.id = m.round_id
            ORDER BY r.sort_order, m.position, p.name
            """
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
        for row in conn.execute("SELECT sort_order FROM rounds ORDER BY sort_order")
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
            GROUP BY r.id
            ORDER BY r.sort_order
            """
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
            WHERE lower(me.name) = lower(?) AND lower(opponent.name) = lower(?)
            ORDER BY r.sort_order, m.position
            """,
            (me, opponent),
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
