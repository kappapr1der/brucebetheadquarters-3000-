#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil


ROOT = Path("/var/lib/brucebet-browser/captures")
ARCHIVE = ROOT / "archive"
RUN_ID = re.compile(r"^(\d{8}T\d{6}Z)-\d+$")
FULL_DAILY_ARTIFACTS = 14
METADATA_DAYS = 30
LOG_LIMIT = 10 * 1024 * 1024
LOG_TAIL = 2 * 1024 * 1024


def target_name(link: Path) -> str | None:
    try:
        target = os.readlink(link)
    except OSError:
        return None
    resolved = (ROOT / target).resolve(strict=True)
    if resolved.parent != ARCHIVE.resolve(strict=True):
        raise RuntimeError(f"unsafe symlink target: {link.name}")
    return resolved.name


def run_time(path: Path) -> datetime:
    match = RUN_ID.fullmatch(path.name)
    if not match:
        raise RuntimeError(f"unexpected capture directory: {path.name}")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def trim_log(path: Path) -> bool:
    try:
        if path.stat().st_size <= LOG_LIMIT:
            return False
        with path.open("rb") as source:
            source.seek(-LOG_TAIL, os.SEEK_END)
            tail = source.read()
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_bytes(tail)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return True
    except FileNotFoundError:
        return False


def main() -> int:
    now = datetime.now(timezone.utc)
    protected = {
        item
        for item in (target_name(ROOT / "latest-complete"), target_name(ROOT / "latest-attempt"))
        if item
    }
    runs = sorted(
        (path for path in ARCHIVE.iterdir() if path.is_dir() and not path.is_symlink()),
        key=run_time,
        reverse=True,
    )
    complete_by_day: dict[str, Path] = {}
    for path in runs:
        if manifest(path).get("capture_complete") is True:
            complete_by_day.setdefault(run_time(path).date().isoformat(), path)
    full = {path.name for path in list(complete_by_day.values())[:FULL_DAILY_ARTIFACTS]} | protected

    removed: list[str] = []
    stripped: list[str] = []
    cutoff = now - timedelta(days=METADATA_DAYS)
    for path in runs:
        if path.name not in protected and run_time(path) < cutoff:
            path.chmod(0o750)
            shutil.rmtree(path)
            removed.append(path.name)
            continue
        if path.name not in full:
            artifact = path / "capture.jsonl"
            sums = path / "SHA256SUMS"
            if artifact.exists() or sums.exists():
                # Successful capture bundles are sealed read-only for the
                # export account. The owner must briefly reopen only this
                # validated run directory before pruning payload files.
                path.chmod(0o750)
                try:
                    if artifact.exists():
                        artifact.unlink()
                        stripped.append(path.name)
                    if sums.exists():
                        sums.unlink()
                finally:
                    path.chmod(0o550)

    trimmed = [
        str(path)
        for path in (
            ROOT / "runtime" / "xvfb.log",
            ROOT / "runtime" / "chrome.stdout.log",
            ROOT / "runtime" / "chrome.stderr.log",
        )
        if trim_log(path)
    ]
    print(
        json.dumps(
            {
                "schema": "brucebet.vk-reader-retention/v1",
                "protected": sorted(protected),
                "full_artifacts": sorted(full),
                "removed": removed,
                "artifact_stripped": stripped,
                "logs_trimmed": trimmed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
