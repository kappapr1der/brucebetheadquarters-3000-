#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


REVISION = "27b9573ee8e499e0ec2699e2cfde94b7e10cb1ba"
IMAGE = "sha256:03a53c02b1acc3670a7e7c5de9dd2a143710886a483aae6ec24590093af6bd78"
CONTAINER = "brucebet-3000"
GROUP_ID = 217130885
TOPIC_ID = 67251746
FLAGS = {
    "VK_API_READ_ENABLED": "0",
    "VK_PREDICTIONS_IMPORT_ENABLED": "0",
    "VK_PREDICTIONS_SNAPSHOT_ENABLED": "0",
}
ROOT = Path("/var/lib/brucebet-vk-pipeline")
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"
LATEST_RESULT = ROOT / "latest-result.json"
LOCK = Path("/run/lock/brucebet-vk-pipeline.lock")
INBOX = Path("/var/lib/brucebet-vk-pull/inbox/latest-complete")
INBOX_ARCHIVE = Path("/var/lib/brucebet-vk-pull/inbox/archive")
PULL_RESULT = Path("/var/lib/brucebet-vk-pull/inbox/last-result.json")
PRODUCTION_DB = Path("/opt/brucebet-3000/data/forecasters.sqlite")
TOOLS = Path("/usr/local/lib/brucebet-vk-pipeline")
INBOX_FILES = ("manifest.json", "capture.jsonl", "verification.json", "SHA256SUMS")
MAX_SOURCE_AGE = timedelta(hours=1)
FULL_RUNS_TO_KEEP = 30
RESULT_DAYS = 90


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{name} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def atomic_json(path: Path, value: object, mode: int = 0o600) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_production() -> dict[str, object]:
    container = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER]))[0]
    env = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
    revision = container["Config"].get("Labels", {}).get("org.opencontainers.image.revision")
    if revision != REVISION:
        raise RuntimeError("production revision drift")
    if container["State"]["Status"] != "running" or container["RestartCount"] != 0:
        raise RuntimeError("production lifecycle drift")
    observed_flags = {key: env.get(key) for key in FLAGS}
    if observed_flags != FLAGS:
        raise RuntimeError("VK flags drift")
    expected_mount = {
        "Source": "/opt/brucebet-3000/data",
        "Destination": "/app/data",
        "RW": True,
    }
    if not any(all(item.get(key) == value for key, value in expected_mount.items()) for item in container["Mounts"]):
        raise RuntimeError("production data mount drift")
    return {
        "container_id": container["Id"],
        "image_id": container["Image"],
        "revision": revision,
        "started_at": container["State"]["StartedAt"],
        "restart_count": container["RestartCount"],
        "flags": observed_flags,
    }


def copy_source(run_dir: Path) -> tuple[Path, dict[str, object], dict[str, str]]:
    source = INBOX.resolve(strict=True)
    if source.parent != INBOX_ARCHIVE.resolve(strict=True):
        raise RuntimeError("latest-complete escaped inbox archive")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("capture_complete") is not True or manifest.get("challenge") is not False:
        raise RuntimeError("inbox capture is incomplete")
    input_dir = run_dir / "input"
    input_dir.mkdir(mode=0o500)
    hashes: dict[str, str] = {}
    for name in INBOX_FILES:
        origin = source / name
        if not origin.is_file() or origin.is_symlink():
            raise RuntimeError(f"unsafe inbox file: {name}")
        destination = input_dir / name
        shutil.copy2(origin, destination)
        os.chmod(destination, 0o400)
        hashes[name] = sha256(destination)
        if hashes[name] != sha256(origin):
            raise RuntimeError(f"staged inbox hash mismatch: {name}")
    return input_dir, manifest, hashes


def check_fresh_pull(manifest: dict[str, object]) -> dict[str, object]:
    result = json.loads(PULL_RESULT.read_text(encoding="utf-8"))
    if result.get("success") is not True:
        raise RuntimeError("latest pull did not succeed")
    if result.get("capture_fingerprint") != manifest.get("capture_fingerprint"):
        raise RuntimeError("pull/inbox fingerprint mismatch")
    if result.get("newest_post_id") != manifest.get("newest_post_id"):
        raise RuntimeError("pull/inbox newest post mismatch")
    pulled_at = parse_time(result.get("pulled_at"), "pulled_at")
    finished_at = parse_time(result.get("source_finished_at"), "source_finished_at")
    observed_at = now()
    if pulled_at > observed_at + timedelta(minutes=5) or finished_at > observed_at + timedelta(minutes=5):
        raise RuntimeError("freshness timestamp is in the future")
    if observed_at - pulled_at > MAX_SOURCE_AGE or observed_at - finished_at > MAX_SOURCE_AGE:
        raise RuntimeError("sanitized source is stale")
    return result


def backup_database(destination: Path) -> dict[str, object]:
    source = sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(target)
    finally:
        target.close()
        source.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in check.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in check.execute("PRAGMA foreign_key_check")]
    finally:
        check.close()
    if integrity != ["ok"] or foreign_keys:
        raise RuntimeError("production backup failed integrity gate")
    return {
        "sha256": sha256(destination),
        "size": destination.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
    }


def docker_base(run_dir: Path, input_dir: Path, extra_mounts: tuple[str, ...] = ()) -> list[str]:
    command = [
        "docker", "run", "--rm", "--init", "--network=none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", f"type=bind,src={input_dir},dst=/inbox,readonly",
        "--mount", f"type=bind,src={run_dir},dst=/audit",
        "--mount", f"type=bind,src={TOOLS},dst=/tools,readonly",
    ]
    for mount in extra_mounts:
        command.extend(("--mount", mount))
    command.extend(("--entrypoint", "python", IMAGE))
    return command


def run_checked(command: list[str], timeout: int = 120) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    result = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if completed.returncode != 0:
        raise RuntimeError(f"stage command failed: {command[-1]}")
    return result


def gate_counts(report: dict[str, object]) -> dict[str, int]:
    rounds = report["per_round"]
    return {
        "new": sum(int(item["new_comments"]) for item in rounds),
        "changed": sum(int(item["changed_comments"]) for item in rounds),
        "unknown": sum(int(item["unknown_participants"]) for item in rounds),
        "quarantine": sum(int(item["quarantine_candidates"]) for item in rounds),
        "mapping_failures": sum(1 for item in rounds if item.get("fixture_mapping_error")),
        "projected_rejected_lines": int(report["projected_import"]["rejected_lines"]),
        "projected_quarantined_lines": int(report["projected_import"]["quarantined_lines"]),
    }


def clean_old_runs() -> None:
    runs = sorted(
        (path for path in RUNS.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in runs[FULL_RUNS_TO_KEEP:]:
        shutil.rmtree(path)
    cutoff = now() - timedelta(days=RESULT_DAYS)
    for path in RESULTS.glob("*.json"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified < cutoff and path.resolve() != LATEST_RESULT.resolve():
            path.unlink()


def main() -> int:
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNS.mkdir(mode=0o700, exist_ok=True)
    RESULTS.mkdir(mode=0o700, exist_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"success": False, "error": "pipeline_locked"}, sort_keys=True))
        return 75

    started_at = now()
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    run_dir = RUNS / run_id
    run_dir.mkdir(mode=0o700)
    stages: dict[str, object] = {}
    summary: dict[str, object] = {
        "schema": "brucebet.vk-scheduler-cycle/v1",
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "success": False,
        "import_decision": "not_reached",
        "stages": stages,
    }
    working_db = run_dir / "working.sqlite"
    try:
        production_before = inspect_production()
        stages["production_preflight"] = {"pass": True, **production_before}
        input_dir, manifest, source_hashes = copy_source(run_dir)
        pull = check_fresh_pull(manifest)
        stages["source"] = {
            "pass": True,
            "capture_complete": True,
            "challenge": False,
            "capture_fingerprint": manifest["capture_fingerprint"],
            "logical_run_id": manifest["run_id"],
            "observed_run_id": pull["source_run_id"],
            "observed_finished_at": pull["source_finished_at"],
            "newest_post_id": manifest["newest_post_id"],
            "newest_visible_timestamp": manifest["newest_visible_timestamp"],
            "record_count": manifest["displayed_total"],
            "source_hashes": source_hashes,
        }
        backup = backup_database(working_db)
        stages["database_backup"] = {"pass": True, **backup}
        reconciliation_dir = run_dir / "reconciliation"
        command = docker_base(run_dir, input_dir) + [
            "/tools/processor.py", "--inbox", "/inbox", "--db", "/audit/working.sqlite",
            "--code-root", "/app", "--output", "/audit/reconciliation",
        ]
        processor_command = run_checked(command)
        report = json.loads((reconciliation_dir / "dry-run-result.json").read_text(encoding="utf-8"))
        counts = gate_counts(report)
        required_flags = (
            "source_validation_pass", "parser_bridge_pass", "production_copy_reconciliation_pass",
            "historical_rounds_noop", "inbox_processor_ready", "scheduler_candidate",
        )
        flags_ok = all(report["flags"].get(name) is True for name in required_flags)
        blockers = {
            key: value
            for key, value in counts.items()
            if key != "new" and value
        }
        stages["reconciliation"] = {
            "pass": flags_ok and not blockers,
            "counts": counts,
            "flags": report["flags"],
            "blockers": blockers,
            "command": processor_command,
        }
        if not flags_ok or blockers:
            summary["import_decision"] = "blocked_by_reconciliation"
            summary["success"] = True
        elif counts["new"]:
            if os.environ.get("BRUCEBET_VK_PIPELINE_ALLOW_IMPORT") != "1":
                summary["import_decision"] = "new_source_requires_approval"
                summary["success"] = True
            else:
                import_output = run_dir / "guarded-import.json"
                command = docker_base(
                    run_dir,
                    input_dir,
                    ("type=bind,src=/opt/brucebet-3000/data,dst=/live",),
                ) + [
                    "/tools/guarded_import.py", "--mode", "apply-new", "--db", "/live/forecasters.sqlite",
                    "--inbox", "/inbox", "--code-root", "/app", "--tools-root", "/tools",
                    "--preflight", "/audit/reconciliation/dry-run-result.json",
                    "--output", "/audit/guarded-import.json",
                ]
                stages["guarded_import"] = run_checked(command)
                import_result = json.loads(import_output.read_text(encoding="utf-8"))
                if import_result.get("pass") is not True or import_result.get("committed") is not True:
                    raise RuntimeError("guarded import did not commit cleanly")
                summary["import_decision"] = "new_source_imported"
                summary["success"] = True
        else:
            import_output = run_dir / "guarded-import-noop.json"
            command = docker_base(run_dir, input_dir) + [
                "/tools/guarded_import.py", "--mode", "verify-noop", "--db", "/audit/working.sqlite",
                "--inbox", "/inbox", "--code-root", "/app", "--tools-root", "/tools",
                "--preflight", "/audit/reconciliation/dry-run-result.json",
                "--output", "/audit/guarded-import-noop.json",
            ]
            stages["guarded_import_noop"] = run_checked(command)
            import_result = json.loads(import_output.read_text(encoding="utf-8"))
            if import_result.get("pass") is not True or import_result.get("total_changes") != 0:
                raise RuntimeError("duplicate import was not a physical no-op")
            summary["import_decision"] = "duplicate_physical_noop"
            summary["success"] = True
        production_after = inspect_production()
        if production_after != production_before:
            raise RuntimeError("production lifecycle/config changed during cycle")
        summary["production_unchanged"] = True
        summary["capture_fingerprint"] = manifest["capture_fingerprint"]
        summary["newest_post_id"] = manifest["newest_post_id"]
        summary["counts"] = counts
    except Exception as exc:
        summary["error"] = str(exc)
    finally:
        try:
            working_db.unlink()
        except FileNotFoundError:
            pass
        finished_at = now()
        summary["finished_at"] = finished_at.isoformat()
        summary["duration_seconds"] = round((finished_at - started_at).total_seconds(), 3)
        atomic_json(run_dir / "cycle-result.json", summary, 0o400)
        result_path = RESULTS / f"{run_id}.json"
        atomic_json(result_path, summary, 0o400)
        atomic_json(LATEST_RESULT, summary, 0o600)
        clean_old_runs()
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
