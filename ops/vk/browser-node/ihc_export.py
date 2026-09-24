#!/usr/bin/env python3
import json
import os
import pathlib
import re
import stat
import sys


ROOT = pathlib.Path("/var/lib/brucebet-browser/captures")
ARCHIVE = ROOT / "archive"
LATEST = ROOT / "latest-complete"
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
RUN_TARGET = re.compile(r"archive/[A-Za-z0-9._-]+")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


def deny(reason):
    print(f"export denied: {reason}", file=sys.stderr)
    raise SystemExit(64)


def regular_file(path, maximum):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        deny("invalid published file")
    return path.read_bytes()


def latest_bundle():
    try:
        link = os.readlink(LATEST)
    except OSError:
        deny("latest complete unavailable")
    if not RUN_TARGET.fullmatch(link):
        deny("invalid latest target")
    archive = ARCHIVE.resolve(strict=True)
    run_dir = (ROOT / link).resolve(strict=True)
    if run_dir.parent != archive:
        deny("latest target escaped archive")
    manifest_bytes = regular_file(run_dir / "manifest.json", MAX_MANIFEST_BYTES)
    artifact_path = run_dir / "capture.jsonl"
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        deny("manifest malformed")
    if manifest.get("schema") != "brucebet.vk-browser-node-capture/v1":
        deny("manifest schema")
    if manifest.get("capture_complete") is not True:
        deny("capture incomplete")
    return manifest, manifest_bytes, artifact_path


def main():
    command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    manifest, manifest_bytes, artifact_path = latest_bundle()
    if command == "manifest":
        sys.stdout.buffer.write(manifest_bytes)
        return 0
    match = re.fullmatch(r"artifact ([0-9a-f]{64})", command)
    if match:
        requested = match.group(1)
        if manifest.get("capture_fingerprint") != requested:
            deny("fingerprint mismatch")
        sys.stdout.buffer.write(regular_file(artifact_path, MAX_ARTIFACT_BYTES))
        return 0
    deny("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
