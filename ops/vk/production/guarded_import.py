#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


GROUP_ID = 217130885
TOPIC_ID = 67251746


class DeferredCommitConnection(sqlite3.Connection):
    defer_commit = True

    def commit(self) -> None:
        if not self.defer_commit:
            super().commit()

    def final_commit(self) -> None:
        self.defer_commit = False
        super().commit()


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_rows(rows: list[dict[str, Any]]) -> dict[str, object]:
    rows.sort(key=canonical)
    data = (json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    return {"count": len(rows), "sha256": sha256(data).hexdigest()}


def query_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql)]


def protected_digest(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "frozen_recommendations": digest_rows(query_rows(conn, "SELECT * FROM contest_recommendations")),
        "completed_predictions": digest_rows(
            query_rows(
                conn,
                """
                SELECT prediction.* FROM predictions prediction
                JOIN matches match ON match.id=prediction.match_id
                JOIN round_reviews review ON review.round_id=match.round_id
                """,
            )
        ),
        "completed_revisions": digest_rows(
            query_rows(
                conn,
                """
                SELECT revision.* FROM prediction_revisions revision
                JOIN matches match ON match.id=revision.match_id
                JOIN round_reviews review ON review.round_id=match.round_id
                """,
            )
        ),
    }


def build_report(processor: Any, inbox: Path, db: Path, code_root: Path):
    bundle = processor.load_source(inbox)
    api = processor.code_api(code_root)
    provisional, _templates = processor.build_report(bundle, api)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        mapping = {
            template.round_name.strip(): api["map_template"](conn, template)
            for template in provisional.templates
        }
        calibrated, calibration = processor.infer_published_timezone(
            bundle, provisional, mapping, conn, api
        )
    finally:
        conn.close()
    report, _templates = processor.build_report(calibrated, api)
    return report, calibration


def select_report(
    conn: sqlite3.Connection,
    report: Any,
    preflight: dict[str, Any],
    importer: Any,
    mode: str,
):
    expected_status = "known_canonical" if mode == "verify-noop" else "new"
    expected_reconciliation = "known" if mode == "verify-noop" else "new"
    selected_meta: dict[tuple[int, str], dict[str, Any]] = {}
    for item in preflight["submissions"]:
        lines = item.get("lines") or []
        if item.get("reconciliation") != expected_reconciliation or not lines:
            continue
        if any(line.get("line_status") != expected_status for line in lines):
            continue
        if mode == "verify-noop":
            event = conn.execute(
                """
                SELECT 1 FROM vk_prediction_notifications
                WHERE source_key=? AND round_name=?
                LIMIT 1
                """,
                (str(item["source_key"]), str(item["round"]).strip()),
            ).fetchone()
            if event is None:
                continue
        key = (int(item["post_id"]), str(item["round"]).strip())
        if key in selected_meta:
            raise RuntimeError(f"duplicate preflight identity: {key}")
        selected_meta[key] = item
    if not selected_meta:
        raise RuntimeError(f"no submissions selected for {mode}")

    selected = []
    evidence = []
    for submission in report.forecast_submissions:
        post_id = int(submission.source_key.rsplit(":", 1)[-1])
        key = (post_id, submission.round_name.strip())
        item = selected_meta.pop(key, None)
        if item is None:
            continue
        participant_id = int(item["participant_id"])
        participant_name = str(item["participant_name"])
        row = conn.execute("SELECT name FROM participants WHERE id=?", (participant_id,)).fetchone()
        if row is None or str(row["name"]) != participant_name:
            raise RuntimeError(f"participant identity drift for post {post_id}")
        if importer._registered_participant(conn, participant_name) != participant_name:
            raise RuntimeError(f"participant is not registered: {participant_name}")
        if len(submission.forecasts) != int(item["forecast_lines"]):
            raise RuntimeError(f"forecast line drift for post {post_id}")
        if mode == "apply-new":
            completed = conn.execute(
                """
                SELECT 1 FROM round_reviews review
                JOIN rounds round ON round.id=review.round_id
                WHERE round.name=? LIMIT 1
                """,
                (submission.round_name.strip(),),
            ).fetchone()
            if completed is not None:
                raise RuntimeError(f"new forecast targets completed round {submission.round_name}")
        selected.append(replace(submission, participant=participant_name))
        evidence.append(
            {
                "post_id": post_id,
                "round": submission.round_name.strip(),
                "participant_id": participant_id,
                "participant": participant_name,
                "forecast_lines": len(submission.forecasts),
                "identity_method": item.get("identity_method"),
            }
        )
    if selected_meta:
        raise RuntimeError(f"parsed report missed selected submissions: {sorted(selected_meta)}")
    return replace(report, forecast_submissions=tuple(selected)), evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("verify-noop", "apply-new"), required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--tools-root", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(args.tools_root.resolve(strict=True)))
    sys.path.insert(0, str(args.code_root.resolve(strict=True)))
    import processor
    from brucebet import vk_prediction_import as importer

    preflight = json.loads(args.preflight.read_text(encoding="utf-8"))
    report, calibration = build_report(processor, args.inbox, args.db, args.code_root)
    if report.content_fingerprint != preflight["source"]["capture_fingerprint"]:
        raise RuntimeError("rebuilt report fingerprint drift")

    conn = sqlite3.connect(args.db, timeout=30, factory=DeferredCommitConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        integrity_before = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        fk_before = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if integrity_before != ["ok"] or fk_before:
            raise RuntimeError("database failed initial integrity gate")
        season = conn.execute("SELECT deadline_lock_minutes FROM seasons WHERE active=1").fetchone()
        if season is None:
            raise RuntimeError("active season unavailable")
        selected, selection = select_report(conn, report, preflight, importer, args.mode)
        forecast_lines = sum(len(item.forecasts) for item in selected.forecast_submissions)
        before_dump = tuple(conn.iterdump())
        before_total_changes = conn.total_changes
        before_protected = protected_digest(conn)
        before_delivery_count = int(
            conn.execute("SELECT COUNT(*) FROM vk_prediction_notification_deliveries").fetchone()[0]
        )
        statements: list[str] = []
        conn.execute("BEGIN IMMEDIATE")
        conn.set_trace_callback(statements.append)
        result = importer.import_vk_prediction_report(
            conn,
            selected,
            expected_group_id=GROUP_ID,
            expected_topic_id=TOPIC_ID,
            lock_minutes=int(season["deadline_lock_minutes"]),
            notification_chat_ids=(),
            recovery_mode=False,
        )
        conn.set_trace_callback(None)
        dml = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE "))
        ]
        integrity_after = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        fk_after = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        after_protected = protected_digest(conn)
        after_delivery_count = int(
            conn.execute("SELECT COUNT(*) FROM vk_prediction_notification_deliveries").fetchone()[0]
        )
        total_changes = conn.total_changes - before_total_changes

        if args.mode == "verify-noop":
            checks = {
                "duplicates_match": result.duplicates == forecast_lines,
                "revisions_created_0": result.revisions_created == 0,
                "decisions_0": (result.accepted, result.rejected, result.quarantined) == (0, 0, 0),
                "notifications_0": result.notification_events_created == 0,
                "total_changes_0": total_changes == 0,
                "dml_0": not dml,
                "dump_identical": before_dump == tuple(conn.iterdump()),
                "protected_identical": before_protected == after_protected,
                "deliveries_identical": before_delivery_count == after_delivery_count,
                "integrity_ok": integrity_after == ["ok"] and not fk_after,
            }
            conn.rollback()
            committed = False
        else:
            checks = {
                "all_revisions_created": result.revisions_created == forecast_lines,
                "duplicates_0": result.duplicates == 0,
                "all_accepted": result.accepted == forecast_lines,
                "rejected_quarantined_0": (result.rejected, result.quarantined) == (0, 0),
                "no_delivery_rows": before_delivery_count == after_delivery_count,
                "protected_identical": before_protected == after_protected,
                "integrity_ok": integrity_after == ["ok"] and not fk_after,
            }
            if all(checks.values()):
                conn.final_commit()
                committed = True
            else:
                conn.rollback()
                committed = False
        passed = all(checks.values())
        payload = {
            "schema": "brucebet.vk-guarded-import/v1",
            "mode": args.mode,
            "pass": passed,
            "committed": committed,
            "capture_fingerprint": preflight["source"]["capture_fingerprint"],
            "selection": selection,
            "forecast_lines": forecast_lines,
            "report": asdict(result),
            "total_changes": total_changes,
            "dml_count": len(dml),
            "checks": checks,
            "calibration": calibration,
            "integrity_check": integrity_after,
            "foreign_key_violations": fk_after,
        }
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"pass": passed, "mode": args.mode, "committed": committed, "checks": checks}, sort_keys=True))
        return 0 if passed else 1
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
