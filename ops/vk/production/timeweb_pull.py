#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import urllib.parse
from zoneinfo import ZoneInfo


SCHEMA = "brucebet.vk-browser-node-capture/v1"
GROUP_ID = 217130885
TOPIC_ID = 67251746
TOPIC_URL = f"https://vk.ru/topic-{GROUP_ID}_{TOPIC_ID}"
REMOTE = "brucebet-reader-export@217.25.89.96"
HOME = pathlib.Path("/var/lib/brucebet-vk-pull")
KEY = HOME / "keys" / "id_ed25519"
KNOWN_HOSTS = HOME / "keys" / "known_hosts"
INBOX = HOME / "inbox"
ARCHIVE = INBOX / "archive"
LATEST = INBOX / "latest-complete"
LAST_RESULT = INBOX / "last-result.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")
VK_DISPLAY_ZONE = ZoneInfo("Europe/Moscow")
VISIBLE_TIME_WITH_YEAR = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[\u0430-\u044f\u0451]{3})\s+(?P<year>\d{4})\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
RELATIVE_VISIBLE_TIME = re.compile(
    r"^(?P<day>\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0432\u0447\u0435\u0440\u0430)\s+\u0432\s+(?P<time>\d{1,2}:\d{2})$",
    re.IGNORECASE,
)
MONTHS = {
    "\u044f\u043d\u0432": 1,
    "\u0444\u0435\u0432": 2,
    "\u043c\u0430\u0440": 3,
    "\u0430\u043f\u0440": 4,
    "\u043c\u0430\u044f": 5,
    "\u0438\u044e\u043d": 6,
    "\u0438\u044e\u043b": 7,
    "\u0430\u0432\u0433": 8,
    "\u0441\u0435\u043d": 9,
    "\u043e\u043a\u0442": 10,
    "\u043d\u043e\u044f": 11,
    "\u0434\u0435\u043a": 12,
}
RECORD_KEYS = {
    "group_id", "topic_id", "post_id", "canonical_permalink",
    "display_author", "public_profile_url", "visible_timestamp",
    "body_text", "edit_status", "observed_at",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized_visible_timestamp(value, observed_at):
    raw = str(value or "").strip()
    match = VISIBLE_TIME_WITH_YEAR.fullmatch(raw)
    if match and (month := MONTHS.get(match.group("month").casefold())):
        hour, minute = (int(part) for part in match.group("time").split(":"))
        wall_time = datetime(
            int(match.group("year")), month, int(match.group("day")), hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    relative = RELATIVE_VISIBLE_TIME.fullmatch(raw)
    if relative:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        local_date = observed.astimezone(VK_DISPLAY_ZONE).date()
        if relative.group("day").casefold() == "\u0432\u0447\u0435\u0440\u0430":
            local_date -= timedelta(days=1)
        hour, minute = (int(part) for part in relative.group("time").split(":"))
        wall_time = datetime(
            local_date.year, local_date.month, local_date.day, hour, minute,
            tzinfo=VK_DISPLAY_ZONE,
        )
        return wall_time.astimezone(timezone.utc).isoformat()
    return raw


def logical_fingerprints(records):
    legacy = []
    absolute_only = []
    normalized = []
    for record in records:
        legacy_item = {key: value for key, value in record.items() if key != "observed_at"}
        absolute_only_item = dict(legacy_item)
        if VISIBLE_TIME_WITH_YEAR.fullmatch(str(absolute_only_item.get("visible_timestamp") or "").strip()):
            absolute_only_item["visible_timestamp"] = normalized_visible_timestamp(
                absolute_only_item.get("visible_timestamp"), record.get("observed_at")
            )
        normalized_item = dict(legacy_item)
        normalized_item["visible_timestamp"] = normalized_visible_timestamp(
            normalized_item.get("visible_timestamp"), record.get("observed_at")
        )
        legacy.append(legacy_item)
        absolute_only.append(absolute_only_item)
        normalized.append(normalized_item)
    suffix = "\n"
    return {
        "normalized-visible-time-v1": sha256((canonical_json(normalized) + suffix).encode("utf-8")),
        "absolute-normalized-relative-raw-v1": sha256(
            (canonical_json(absolute_only) + suffix).encode("utf-8")
        ),
        "legacy-raw-visible-time-v1": sha256((canonical_json(legacy) + suffix).encode("utf-8")),
    }


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def atomic_symlink(target, link):
    temporary = link.parent / f".{link.name}.tmp-{os.getpid()}"
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target, temporary)
    os.replace(temporary, link)


def ssh_fetch(command, maximum):
    completed = subprocess.run(
        [
            "/usr/bin/ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "ConnectTimeout=10",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=1",
            "-i", str(KEY),
            REMOTE,
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("ssh_fetch_failed")
    if len(completed.stdout) == 0 or len(completed.stdout) > maximum:
        raise RuntimeError("ssh_payload_size_invalid")
    return completed.stdout


def verify_manifest(data):
    if len(data) > MAX_MANIFEST_BYTES:
        raise RuntimeError("manifest_too_large")
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest_malformed") from exc
    required = {
        "schema", "run_id", "group_id", "topic_id", "capture_complete",
        "challenge", "pagination_exhausted", "stop_reason", "displayed_total",
        "unique_canonical_post_count", "newest_post_id",
        "newest_visible_timestamp", "artifact_sha256", "capture_fingerprint", "finished_at",
    }
    if not required.issubset(manifest):
        raise RuntimeError("manifest_fields_missing")
    if manifest["schema"] != SCHEMA:
        raise RuntimeError("manifest_schema_mismatch")
    if manifest["group_id"] != GROUP_ID or manifest["topic_id"] != TOPIC_ID:
        raise RuntimeError("manifest_source_mismatch")
    if manifest["capture_complete"] is not True or manifest["challenge"] is not False:
        raise RuntimeError("manifest_capture_incomplete")
    if manifest["pagination_exhausted"] is not True or manifest["stop_reason"] != "displayed_total_exhausted":
        raise RuntimeError("manifest_completeness_unproven")
    if not HEX64.fullmatch(str(manifest["artifact_sha256"])):
        raise RuntimeError("manifest_artifact_hash_invalid")
    if not HEX64.fullmatch(str(manifest["capture_fingerprint"])):
        raise RuntimeError("manifest_fingerprint_invalid")
    if not RUN_ID.fullmatch(str(manifest["run_id"])):
        raise RuntimeError("manifest_run_id_invalid")
    if not isinstance(manifest["displayed_total"], int) or manifest["displayed_total"] <= 0:
        raise RuntimeError("manifest_total_invalid")
    try:
        finished_at = datetime.fromisoformat(str(manifest["finished_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("manifest_finished_at_invalid") from exc
    if finished_at.tzinfo is None or finished_at.utcoffset() is None:
        raise RuntimeError("manifest_finished_at_invalid")
    return manifest


def verify_artifact(data, manifest):
    if len(data) > MAX_ARTIFACT_BYTES:
        raise RuntimeError("artifact_too_large")
    if sha256(data) != manifest["artifact_sha256"]:
        raise RuntimeError("artifact_sha256_mismatch")
    records = []
    try:
        for line in data.decode("utf-8").splitlines():
            if line:
                records.append(json.loads(line))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("artifact_jsonl_malformed") from exc
    ids = []
    for record in records:
        if set(record) != RECORD_KEYS:
            raise RuntimeError("artifact_record_allowlist_mismatch")
        if record["group_id"] != GROUP_ID or record["topic_id"] != TOPIC_ID:
            raise RuntimeError("artifact_record_source_mismatch")
        post_id = record["post_id"]
        if not isinstance(post_id, int) or post_id <= 0:
            raise RuntimeError("artifact_post_id_invalid")
        ids.append(post_id)
        if record["canonical_permalink"] != f"{TOPIC_URL}?post={post_id}":
            raise RuntimeError("artifact_permalink_invalid")
        profile = urllib.parse.urlsplit(str(record["public_profile_url"]))
        if profile.scheme != "https" or profile.hostname != "vk.ru" or profile.query or profile.fragment:
            raise RuntimeError("artifact_profile_url_invalid")
        for key in ("display_author", "visible_timestamp", "body_text", "observed_at"):
            if not isinstance(record[key], str) or not record[key].strip():
                raise RuntimeError("artifact_required_value_missing")
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise RuntimeError("artifact_duplicate_or_unordered_ids")
    if len(records) != manifest["displayed_total"] or len(records) != manifest["unique_canonical_post_count"]:
        raise RuntimeError("artifact_count_mismatch")
    if ids[-1] != manifest["newest_post_id"]:
        raise RuntimeError("artifact_newest_post_mismatch")
    if records[-1]["visible_timestamp"] != manifest["newest_visible_timestamp"]:
        raise RuntimeError("artifact_newest_timestamp_mismatch")
    fingerprints = logical_fingerprints(records)
    if manifest["capture_fingerprint"] not in fingerprints.values():
        raise RuntimeError("artifact_fingerprint_mismatch")
    return records


def accepted_fingerprint():
    try:
        manifest = json.loads((LATEST / "manifest.json").read_text(encoding="utf-8"))
        return manifest.get("capture_fingerprint")
    except (OSError, ValueError):
        return None


def publish(manifest_data, artifact_data, manifest):
    ARCHIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_name = f"{manifest['run_id']}-{manifest['capture_fingerprint'][:12]}"
    final = ARCHIVE / run_name
    if final.exists():
        raise RuntimeError("inbox_run_collision")
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=".tmp-", dir=INBOX))
    try:
        atomic_write(temporary / "manifest.json", manifest_data, 0o400)
        atomic_write(temporary / "capture.jsonl", artifact_data, 0o400)
        verification = {
            "schema": "brucebet.vk-ssh-pull-verification/v1",
            "capture_fingerprint": manifest["capture_fingerprint"],
            "artifact_sha256": manifest["artifact_sha256"],
            "record_count": manifest["displayed_total"],
            "group_id": GROUP_ID,
            "topic_id": TOPIC_ID,
            "verified": True,
        }
        verification_data = (json.dumps(verification, indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write(temporary / "verification.json", verification_data, 0o400)
        sums = (
            f"{sha256(artifact_data)}  capture.jsonl\n"
            f"{sha256(manifest_data)}  manifest.json\n"
            f"{sha256(verification_data)}  verification.json\n"
        ).encode("ascii")
        atomic_write(temporary / "SHA256SUMS", sums, 0o400)
        os.replace(temporary, final)
        final.chmod(0o500)
        atomic_symlink(pathlib.Path("archive") / run_name, LATEST)
        return final
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def write_result(result):
    atomic_write(
        LAST_RESULT,
        (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        0o600,
    )


def run():
    manifest_data = ssh_fetch("manifest", MAX_MANIFEST_BYTES)
    manifest = verify_manifest(manifest_data)
    artifact_data = ssh_fetch(f"artifact {manifest['capture_fingerprint']}", MAX_ARTIFACT_BYTES)
    records = verify_artifact(artifact_data, manifest)
    previous = accepted_fingerprint()
    duplicate = previous == manifest["capture_fingerprint"]
    destination = None
    if not duplicate:
        destination = publish(manifest_data, artifact_data, manifest)
    result = {
        "schema": "brucebet.vk-ssh-pull-result/v1",
        "success": True,
        "duplicate_noop": duplicate,
        "capture_fingerprint": manifest["capture_fingerprint"],
        "source_run_id": manifest["run_id"],
        "source_finished_at": manifest.get("finished_at"),
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "artifact_sha256": manifest["artifact_sha256"],
        "record_count": len(records),
        "newest_post_id": manifest["newest_post_id"],
        "newest_visible_timestamp": manifest["newest_visible_timestamp"],
        "published_directory": str(destination) if destination else None,
    }
    write_result(result)
    print(json.dumps(result, sort_keys=True))
    return 0


def main():
    try:
        return run()
    except Exception as exc:
        result = {
            "schema": "brucebet.vk-ssh-pull-result/v1",
            "success": False,
            "duplicate_noop": False,
            "error": str(exc),
        }
        try:
            write_result(result)
        except Exception:
            pass
        print(json.dumps(result, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
