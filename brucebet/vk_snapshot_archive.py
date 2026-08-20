from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
import json
from pathlib import Path
import tempfile
from typing import Any

from .vk_dry_run import VkPublicTopicCapture


@dataclass(frozen=True)
class VkSnapshotArchiveResult:
    path: Path
    latest_path: Path
    created: bool


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(payload)
    temporary_path.replace(path)


def archive_public_topic_capture(out_dir: str | Path, capture: VkPublicTopicCapture) -> VkSnapshotArchiveResult:
    """Archive changed public-topic reads outside SQLite and outside Git."""

    report = capture.report
    topic_dir = Path(out_dir) / f"topic-{report.topic_id}"
    latest_path = topic_dir / "latest.json"
    fingerprint = report.content_fingerprint

    if latest_path.exists():
        try:
            current = json.loads(latest_path.read_text(encoding="utf-8"))
            if current.get("content_fingerprint") == fingerprint:
                return VkSnapshotArchiveResult(path=latest_path, latest_path=latest_path, created=False)
        except (OSError, json.JSONDecodeError):
            pass

    captured_at = report.captured_at.astimezone().strftime("%Y%m%dT%H%M%S%z")
    archive_path = topic_dir / f"{captured_at}-{fingerprint[:16]}.json"
    document = {
        "schema_version": 1,
        "captured_at": report.captured_at.isoformat(),
        "content_fingerprint": fingerprint,
        "source": {
            "reader": "public_chromium",
            "url": report.url,
            "html_chars": capture.html_chars,
            "visible_chars": capture.visible_chars,
            "score_line_count": capture.score_line_count,
        },
        "visible_text": capture.visible_text,
        "report": _json_value(report),
    }
    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_atomic(archive_path, payload)
    _write_atomic(latest_path, payload)
    return VkSnapshotArchiveResult(path=archive_path, latest_path=latest_path, created=True)
