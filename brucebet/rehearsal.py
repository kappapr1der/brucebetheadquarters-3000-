from __future__ import annotations

from datetime import datetime

from .analytics import capture_model_forecasts, compute_standings, finalize_completed_rounds, model_calibration_summary, round_review
from .storage import connect, reset_db, upsert_match, upsert_match_assessment, upsert_prediction


def run_rehearsal(lock_minutes: int = 90) -> dict[str, object]:
    """Exercise the contest loop in an in-memory database without touching live data."""
    conn = connect(":memory:")
    try:
        reset_db(conn)
        kickoff = "2026-08-15T17:00:00+03:00"
        upsert_match(conn, "TEST", 1, "Arsenal", "Chelsea", kickoff, "2:1", "2026-08-15T15:30:00+03:00")
        upsert_match(conn, "TEST", 2, "Liverpool", "Everton", "2026-08-15T19:30:00+03:00", "1:1")
        upsert_prediction(conn, "Bruce Wayne", "TEST", 1, "2:1", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_prediction(conn, "Bruce Wayne", "TEST", 2, "2:1", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_prediction(conn, "Igor", "TEST", 1, "1:0", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_prediction(conn, "Igor", "TEST", 2, "1:1", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_prediction(conn, "Anna", "TEST", 1, "2-1", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_prediction(conn, "Anna", "TEST", 2, "0:0", "2026-08-15T14:00:00+03:00", "rehearsal")
        upsert_match_assessment(
            conn,
            {
                "round": "TEST",
                "position": "1",
                "suggested_score": "2:1",
                "risk_level": "medium",
                "confidence": "0.62",
                "updated_at": "2026-08-14T12:00:00+03:00",
            },
        )
        upsert_match_assessment(
            conn,
            {
                "round": "TEST",
                "position": "2",
                "suggested_score": "1:0",
                "risk_level": "low",
                "confidence": "0.71",
                "updated_at": "2026-08-14T12:00:00+03:00",
            },
        )
        captured = capture_model_forecasts(conn, now=datetime.fromisoformat("2026-08-14T18:00:00+03:00"))
        saved = finalize_completed_rounds(conn, lock_minutes=lock_minutes)
        review = round_review(conn, "TEST", lock_minutes=lock_minutes)
        calibration = model_calibration_summary(conn, round_name="TEST")
        standings = compute_standings(conn, lock_minutes=lock_minutes)
        return {
            "captured": captured,
            "reviews_saved": len(saved),
            "review": review,
            "calibration": calibration,
            "standings": [{"rank": row.rank, "name": row.name, "points": row.total} for row in standings],
        }
    finally:
        conn.close()
