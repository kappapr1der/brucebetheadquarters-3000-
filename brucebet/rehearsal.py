from __future__ import annotations

from datetime import datetime, timedelta

from .analytics import (
    capture_model_forecasts,
    compute_standings,
    edge_map,
    field_summary,
    finalize_completed_rounds,
    missing_forecasts_summary,
    model_calibration_summary,
    ready_summary,
    round_review,
)
from .forecast_import import import_forecast_block, import_participant_block
from .reminders import due_reminders, mark_delivery_sent, subscribe_chat
from .storage import (
    connect,
    manual_prediction_history,
    reset_db,
    set_manual_prediction_override,
    upsert_match,
    upsert_match_assessment,
    upsert_match_odds,
)


FIXTURES = (
    ("Arsenal", "Chelsea", "2:1"),
    ("Liverpool", "Everton", "1:1"),
    ("Manchester City", "Tottenham", "3:1"),
    ("Newcastle", "Aston Villa", "1:0"),
    ("Brighton", "West Ham", "2:0"),
    ("Brentford", "Crystal Palace", "1:1"),
    ("Fulham", "Bournemouth", "0:1"),
    ("Leeds", "Wolves", "2:1"),
    ("Nottingham Forest", "Sunderland", "0:0"),
    ("Burnley", "Manchester United", "0:2"),
)

FORECAST_BLOCKS = {
    "Bruce Wayne": "2:1\n2:1\n3:1\n1:0\n2:0\n1:1\n0:1\n2:1\n0:0\n0:2",
    "Igor": "1:0\n2:0\n2:1\n1:0\n1:0\n1:1\n1:1\n1:0\n0:0\n0:2",
    "Anna": "2:1\n1:1\n2:1\n2:1\n2:0\n0:0\n1:0\n2:1\n1:1\n1:2",
    "Mikhail": "3:1\n2:1\n3:1\n1:1\n1:0\n2:1\n0:1\n1:1\n0:0\n0:2",
    "Stas": "1:1\n1:1\n2:2\n0:1\n2:0\n1:1\n0:0\n2:1\n1:0\n1:2",
}


def _check(name: str, passed: bool, detail: str) -> dict[str, object]:
    return {"name": name, "passed": passed, "detail": detail}


def run_rehearsal(lock_minutes: int = 90) -> dict[str, object]:
    """Exercise a full ten-match contest round without touching live data."""
    conn = connect(":memory:")
    try:
        reset_db(conn)
        round_name = "REHEARSAL"
        first_kickoff = datetime.fromisoformat("2027-08-14T17:00:00+03:00")
        deadline = first_kickoff - timedelta(minutes=lock_minutes)
        match_ids: list[int] = []
        for position, (home, away, result) in enumerate(FIXTURES, start=1):
            kickoff = first_kickoff + timedelta(minutes=(position - 1) * 90)
            match_ids.append(
                upsert_match(
                    conn,
                    round_name,
                    position,
                    home,
                    away,
                    kickoff.isoformat(),
                    result,
                    deadline.isoformat(),
                )
            )
            upsert_match_assessment(
                conn,
                {
                    "round": round_name,
                    "position": str(position),
                    "suggested_score": "2:1" if position % 3 else "1:1",
                    "risk_level": "medium" if position % 2 else "low",
                    "confidence": "0.64",
                    "home_edge": "0.50",
                    "draw_edge": "0.23",
                    "away_edge": "0.27",
                    "volatility": "0.35",
                    "contrarian_note": "Rehearsal signal only.",
                    "updated_at": (deadline - timedelta(hours=5)).isoformat(),
                },
            )
            upsert_match_odds(
                conn,
                {
                    "round": round_name,
                    "position": str(position),
                    "bookmaker": "rehearsal-market",
                    "captured_at": (deadline - timedelta(hours=4)).isoformat(),
                    "home_win": "1.85",
                    "draw": "3.60",
                    "away_win": "4.20",
                    "under_2_5": "1.95",
                    "over_2_5": "1.90",
                },
            )

        participant_report = import_participant_block(
            conn,
            "Bruce Wayne 300р\nIgor 300р\nAnna 300р\nMikhail 300р\nStas без взноса",
        )
        submitted_at = deadline - timedelta(hours=2)
        forecast_reports = [
            import_forecast_block(
                conn,
                participant=participant,
                round_name=round_name,
                text=block,
                submitted_at=submitted_at,
                source="rehearsal-direct-input",
                lock_minutes=lock_minutes,
            )
            for participant, block in FORECAST_BLOCKS.items()
        ]

        ready = ready_summary(
            conn,
            user_participant="Bruce Wayne",
            lock_minutes=lock_minutes,
            now=deadline - timedelta(hours=1),
        )
        missing = missing_forecasts_summary(conn, round_name, lock_minutes=lock_minutes)
        first_field = field_summary(conn, match_ids[0])
        edge = edge_map(conn, round_name)

        subscribe_chat(conn, 999, now=deadline - timedelta(days=2))
        deliveries = due_reminders(
            conn,
            now=deadline - timedelta(minutes=20),
            lock_minutes=lock_minutes,
        )
        for delivery in deliveries:
            mark_delivery_sent(conn, delivery.delivery_id, now=deadline - timedelta(minutes=20))

        protected = import_forecast_block(
            conn,
            participant="Bruce Wayne",
            round_name=round_name,
            text="3:0",
            submitted_at=deadline + timedelta(minutes=5),
            source="rehearsal-late-replace",
            lock_minutes=lock_minutes,
        )
        previous, current = set_manual_prediction_override(
            conn,
            "Bruce Wayne",
            match_ids[0],
            "2:0",
            actor_chat_id=999,
            reason="rehearsal audit check",
            changed_at=(deadline + timedelta(minutes=6)).isoformat(),
        )
        override_history = manual_prediction_history(conn, "Bruce Wayne", match_ids[0])

        captured = capture_model_forecasts(conn, now=deadline - timedelta(days=1))
        saved = finalize_completed_rounds(conn, lock_minutes=lock_minutes)
        review = round_review(conn, round_name, lock_minutes=lock_minutes)
        calibration = model_calibration_summary(conn, round_name=round_name)
        standings = compute_standings(conn, lock_minutes=lock_minutes)
        stored_forecasts = sum(report.stored_count for report in forecast_reports)
        checks = [
            _check(
                "participant intake",
                participant_report.accepted_count == len(FORECAST_BLOCKS),
                f"{participant_report.accepted_count}/{len(FORECAST_BLOCKS)} participants accepted",
            ),
            _check(
                "forecast intake",
                stored_forecasts == len(FIXTURES) * len(FORECAST_BLOCKS),
                f"{stored_forecasts}/{len(FIXTURES) * len(FORECAST_BLOCKS)} forecast rows stored",
            ),
            _check(
                "missing report",
                not missing["incomplete"],
                f"{missing['complete_count']}/{missing['participant_count']} complete",
            ),
            _check(
                "field and edge",
                sum(first_field["outcomes"].values()) == len(FORECAST_BLOCKS) and len(edge["opportunities"]) == len(FIXTURES),
                f"field={sum(first_field['outcomes'].values())}, edge={len(edge['opportunities'])}",
            ),
            _check(
                "deadline reminder",
                any(delivery.reminder_key == "deadline_minus_20m" for delivery in deliveries),
                f"{len(deliveries)} scheduled reminder(s) delivered",
            ),
            _check(
                "late replacement protection",
                protected.protected_positions == (1,),
                "late replacement for position 1 was kept out",
            ),
            _check(
                "manual forecast audit",
                previous == "2:1" and current == "2:0" and len(override_history) == 1,
                "explicit correction was recorded with its reason",
            ),
        ]
        return {
            "captured": captured,
            "reviews_saved": len(saved),
            "review": review,
            "calibration": calibration,
            "standings": [{"rank": row.rank, "name": row.name, "points": row.total} for row in standings],
            "ready": ready,
            "missing": missing,
            "checks": checks,
        }
    finally:
        conn.close()
