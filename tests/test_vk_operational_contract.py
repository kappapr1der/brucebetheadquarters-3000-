"""Offline contracts for the deployed VK operational scripts (no VK or DB access)."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import ast
import configparser
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


OPS = Path(__file__).resolve().parents[1] / "ops" / "vk"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if sys.platform == "win32" and name == "operational_pipeline":
        fcntl = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_: None)
        with patch.dict(sys.modules, {"fcntl": fcntl}):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


reader = load("operational_reader", OPS / "browser-node" / "reader.py")
exporter = load("operational_exporter", OPS / "browser-node" / "ihc_export.py")
pull = load("operational_pull", OPS / "production" / "timeweb_pull.py")
processor = load("operational_processor", OPS / "production" / "processor.py")
pipeline = load("operational_pipeline", OPS / "production" / "pipeline.py")
retention = load("operational_retention", OPS / "production" / "pull_retention.py")


def synthetic_records(count, relative=False):
    observed = "2026-09-18T14:15:27Z"
    records = []
    for post_id in range(1, count + 1):
        records.append({
            "group_id": pull.GROUP_ID,
            "topic_id": pull.TOPIC_ID,
            "post_id": post_id,
            "canonical_permalink": f"{pull.TOPIC_URL}?post={post_id}",
            "display_author": f"Synthetic User {post_id}",
            "public_profile_url": "https://vk.ru/test.user",
            "visible_timestamp": "сегодня в 16:51" if relative and post_id == count else "18 сен 2026 в 10:13",
            "body_text": "Team A - Team B 1:0",
            "edit_status": "not_observed",
            "observed_at": observed,
        })
    return records


def bundle(count, fingerprint_contract, relative=False):
    records = synthetic_records(count, relative)
    artifact = "".join(pull.canonical_json(row) + "\n" for row in records).encode("utf-8")
    fingerprint = pull.logical_fingerprints(records)[fingerprint_contract]
    manifest = {
        "schema": pull.SCHEMA,
        "run_id": "20260918T141527Z-12345",
        "group_id": pull.GROUP_ID,
        "topic_id": pull.TOPIC_ID,
        "capture_complete": True,
        "challenge": False,
        "pagination_exhausted": True,
        "stop_reason": "displayed_total_exhausted",
        "displayed_total": count,
        "record_count": count,
        "unique_canonical_post_count": count,
        "newest_post_id": count,
        "newest_visible_timestamp": records[-1]["visible_timestamp"],
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "capture_fingerprint": fingerprint,
        "finished_at": "2026-09-18T14:15:30Z",
    }
    manifest_data = (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
    verification = {
        "schema": processor.VERIFICATION_SCHEMA,
        "verified": True,
        "group_id": pull.GROUP_ID,
        "topic_id": pull.TOPIC_ID,
        "artifact_sha256": manifest["artifact_sha256"],
        "capture_fingerprint": fingerprint,
        "record_count": count,
    }
    verification_data = (json.dumps(verification, sort_keys=True) + "\n").encode("utf-8")
    sums = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n"
        for name, data in (
            ("manifest.json", manifest_data),
            ("capture.jsonl", artifact),
            ("verification.json", verification_data),
        )
    ).encode("ascii")
    return manifest, artifact, (manifest_data, artifact, verification_data, sums)


class FingerprintContracts(unittest.TestCase):
    def test_documented_inventory_matches_local_source_bytes(self):
        inventory = (OPS / "README.md").read_text(encoding="utf-8").split(
            "## Local candidate, not installed", 1
        )[0]
        listed = 0
        for line in inventory.splitlines():
            if not line.startswith(("| Browser Node |", "| Production |")):
                continue
            fields = re.findall(r"`([^`]+)`", line)
            self.assertEqual(3, len(fields), line)
            deployed, relative_path, expected_hash = fields
            self.assertTrue(deployed.startswith(("/usr/local/lib/", "/etc/systemd/system/")))
            actual_hash = hashlib.sha256((OPS / relative_path).read_bytes()).hexdigest()
            self.assertEqual(expected_hash, actual_hash, relative_path)
            listed += 1
        self.assertEqual(22, listed)

    def test_legacy_57_hybrid_66_and_normalized_67(self):
        cases = (
            (57, "legacy-raw-visible-time-v1", False),
            (66, "absolute-normalized-relative-raw-v1", True),
            (67, "normalized-visible-time-v1", True),
        )
        for count, contract, relative in cases:
            with self.subTest(contract=contract, count=count):
                manifest, artifact, payloads = bundle(count, contract, relative)
                self.assertEqual(count, len(pull.verify_artifact(artifact, manifest)))
                self.assertEqual(count, len(processor.validate_payloads(*payloads).records))

    def test_relative_moscow_normalization_is_host_timezone_independent(self):
        observed = "2026-09-18T14:15:27Z"
        for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
            with self.subTest(zone=zone), patch.dict(os.environ, {"TZ": zone}):
                self.assertEqual(
                    "2026-09-18T13:51:00+00:00",
                    reader.normalized_visible_timestamp("сегодня в 16:51", observed),
                )
                self.assertEqual(
                    "2026-09-18T13:51:00+00:00",
                    pull.normalized_visible_timestamp("сегодня в 16:51", observed),
                )
                self.assertEqual(
                    "2026-09-17T07:13:00+00:00",
                    processor.normalized_visible_timestamp("вчера в 10:13", observed),
                )

    def test_tampered_artifact_or_fingerprint_is_rejected(self):
        manifest, artifact, _ = bundle(67, "normalized-visible-time-v1", True)
        tampered = artifact.replace(b"Team A", b"Team X", 1)
        with self.assertRaisesRegex(RuntimeError, "artifact_sha256_mismatch"):
            pull.verify_artifact(tampered, manifest)
        changed = dict(manifest, artifact_sha256=hashlib.sha256(tampered).hexdigest())
        with self.assertRaisesRegex(RuntimeError, "artifact_fingerprint_mismatch"):
            pull.verify_artifact(tampered, changed)


class ReaderPublication(unittest.TestCase):
    def test_complete_duplicate_refreshes_pointer_but_challenge_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = {}

            def portable_pointer(target, link):
                if link.exists():
                    link.chmod(0o700)
                    for child in link.iterdir():
                        child.chmod(0o600)
                    shutil.rmtree(link)
                shutil.copytree(root / target, link)
                targets[link.name] = target.as_posix()

            pointer = portable_pointer if sys.platform == "win32" else reader.atomic_symlink
            with patch.object(reader, "atomic_symlink", pointer):
                results = []
                for minute in (0, 10, 20):
                    records = synthetic_records(1)
                    records[0]["observed_at"] = f"2026-09-18T14:{minute:02d}:27Z"
                    run_id = f"20260918T14{minute:02d}27Z-12345"
                    manifest = {
                        "schema": reader.SCHEMA,
                        "run_id": run_id,
                        "capture_complete": True,
                        "displayed_total": 1,
                        "finished_at": records[0]["observed_at"],
                    }
                    results.append(reader.write_bundle(manifest, records, root=root))
                self.assertEqual([True, False, False], [r["logical_event_created"] for r in results])
                self.assertTrue(all(r["latest_complete_updated"] for r in results))
                self.assertEqual(1, len({r["capture_fingerprint"] for r in results}))
                before = (root / "archive" / "20260918T140027Z-12345" / "capture.jsonl").read_bytes()
                failed = reader.write_bundle(
                    {"run_id": "20260918T143027Z-12345", "capture_complete": False,
                     "challenge": True, "displayed_total": 0},
                    [], root=root,
                )
                self.assertFalse(failed["latest_complete_updated"])
                self.assertEqual(before, (root / "archive" / "20260918T140027Z-12345" / "capture.jsonl").read_bytes())
                self.assertEqual(
                    "20260918T142027Z-12345",
                    json.loads((root / "latest-complete" / "manifest.json").read_text())["run_id"],
                )

            def current_target(path):
                return targets[path.name] if sys.platform == "win32" else os.readlink(path)

            with ExitStack() as stack:
                stack.enter_context(patch.object(exporter, "ROOT", root))
                stack.enter_context(patch.object(exporter, "ARCHIVE", root / "archive"))
                stack.enter_context(patch.object(exporter, "LATEST", root / "latest-complete"))
                if sys.platform == "win32":
                    stack.enter_context(patch.object(exporter.os, "readlink", current_target))
                exported, _, _ = exporter.latest_bundle()
                self.assertEqual("20260918T142027Z-12345", exported["run_id"])
                with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "cat /etc/passwd"}):
                    with self.assertRaises(SystemExit) as denied, redirect_stderr(io.StringIO()):
                        exporter.main()
                self.assertEqual(64, denied.exception.code)
                with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "artifact ../../etc/passwd"}):
                    with self.assertRaises(SystemExit) as denied, redirect_stderr(io.StringIO()):
                        exporter.main()
                self.assertEqual(64, denied.exception.code)


class RetentionAndScheduler(unittest.TestCase):
    def test_reader_timer_candidate_has_bounded_ordered_windows(self):
        units = OPS / "systemd"

        def unit(path):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read(path, encoding="utf-8")
            return parser

        def minutes(calendar):
            match = re.fullmatch(r"\*-\*-\* \*:(\d{2}(?:,\d{2})*):00", calendar)
            self.assertIsNotNone(match, calendar)
            return tuple(int(value) for value in match.group(1).split(","))

        reader_timer_path = units / "browser-node" / "brucebet-vk-reader.timer"
        inventory = (OPS / "README.md").read_text(encoding="utf-8")
        candidate_hash = hashlib.sha256(reader_timer_path.read_bytes()).hexdigest()
        self.assertIn(
            f"`systemd/browser-node/brucebet-vk-reader.timer` | `{candidate_hash}`",
            inventory,
        )
        reader_timer = unit(reader_timer_path)["Timer"]
        pull_timer = unit(units / "production" / "brucebet-vk-pull.timer")["Timer"]
        pipeline_timer = unit(units / "production" / "brucebet-vk-pipeline.timer")["Timer"]
        reader_service = unit(units / "browser-node" / "brucebet-vk-reader.service")["Service"]
        pull_service = unit(units / "production" / "brucebet-vk-pull.service")["Service"]

        self.assertEqual("brucebet-vk-reader.service", reader_timer["Unit"])
        self.assertEqual("yes", reader_timer["FixedRandomDelay"])
        self.assertEqual("false", reader_timer["Persistent"])
        self.assertEqual("1s", reader_timer["AccuracySec"])
        self.assertEqual("120s", reader_timer["RandomizedDelaySec"])
        self.assertFalse(any(key.lower().startswith(("onunit", "onboot", "onfailure"))
                             for key in reader_timer))
        self.assertIn("kernel_oom_check.py pre", reader_service["ExecStartPre"])
        reader_dropin = (units / "browser-node" / "10-retention.conf").read_text(encoding="utf-8")
        self.assertIn("/usr/bin/flock --nonblock", reader_dropin)
        self.assertIn("reader_retention.py", reader_dropin)

        capture = minutes(reader_timer["OnCalendar"])
        pull = minutes(pull_timer["OnCalendar"])
        reconcile = minutes(pipeline_timer["OnCalendar"])
        self.assertEqual((0, 20, 40), capture)
        self.assertEqual((7, 27, 47), pull)
        self.assertEqual((9, 29, 49), reconcile)
        reader_bound = int(reader_service["TimeoutStartSec"]) + int(reader_service["TimeoutStopSec"])
        pull_bound = int(pull_service["TimeoutStartSec"]) + int(pull_service["TimeoutStopSec"])
        jitter = int(reader_timer["RandomizedDelaySec"].removesuffix("s"))
        for first, second, third in zip(capture, pull, reconcile):
            with self.subTest(capture_minute=first):
                self.assertLess(first, second)
                self.assertLess(second, third)
                capture_to_pull = (second - first) * 60 - jitter - 1 - reader_bound
                pull_to_reconcile = (third - second) * 60 - 1 - pull_bound
                self.assertGreaterEqual(capture_to_pull, 30)
                self.assertGreaterEqual(pull_to_reconcile, 30)

    @unittest.skipUnless(os.name == "posix", "POSIX sealed-directory permission test")
    def test_sealed_retention_preserves_latest_and_reseals_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive"
            archive.mkdir()
            runs = []
            for index in range(33):
                run = archive / f"202609{index + 1:02d}T120000Z-12345-{'a' * 12}"
                run.mkdir()
                for name in ("manifest.json", "capture.jsonl", "verification.json", "SHA256SUMS"):
                    child = run / name
                    child.write_text(name, encoding="utf-8")
                    child.chmod(0o400)
                run.chmod(0o500)
                runs.append(run)
            os.symlink(Path("archive") / runs[0].name, root / "latest-complete")
            with patch.multiple(retention, ROOT=root, ARCHIVE=archive, LATEST=root / "latest-complete", KEEP=30):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(0, retention.main())
                removed = json.loads(output.getvalue())["removed"]
                self.assertEqual([runs[2].name, runs[1].name], removed)
                self.assertTrue(runs[0].exists())
                self.assertFalse(runs[1].exists())
                self.assertEqual(0o500, stat.S_IMODE(runs[3].stat().st_mode))
                self.assertEqual(0o400, stat.S_IMODE((runs[3] / "manifest.json").stat().st_mode))
                with redirect_stdout(io.StringIO()) as second:
                    self.assertEqual(0, retention.main())
                self.assertEqual([], json.loads(second.getvalue())["removed"])
                outside = root / "outside"
                outside.mkdir()
                with self.assertRaises(RuntimeError):
                    retention.validated_run(archive / ".." / "outside")
                os.symlink(outside, archive / "symlink-run")
                with self.assertRaises(RuntimeError):
                    retention.validated_run(archive / "symlink-run")
                failure = archive / "failure-run"
                failure.mkdir()
                child = failure / "manifest.json"
                child.write_text("test", encoding="utf-8")
                child.chmod(0o400)
                failure.chmod(0o500)
                with patch.object(retention.shutil, "rmtree", side_effect=RuntimeError("injected failure")):
                    with self.assertRaisesRegex(RuntimeError, "injected failure"):
                        retention.remove_run(failure)
                self.assertEqual(0o500, stat.S_IMODE(failure.stat().st_mode))
                self.assertEqual(0o400, stat.S_IMODE(child.stat().st_mode))
                for run in archive.iterdir():
                    if run.is_dir() and not run.is_symlink():
                        run.chmod(0o700)
                        for item in run.iterdir():
                            item.chmod(0o600)

    def test_observation_mode_does_not_run_guarded_import(self):
        flags = {
            name: True for name in (
                "source_validation_pass", "parser_bridge_pass", "production_copy_reconciliation_pass",
                "historical_rounds_noop", "inbox_processor_ready", "scheduler_candidate",
            )
        }
        report = {
            "flags": flags,
            "per_round": [{"new_comments": 13, "changed_comments": 0, "unknown_participants": 0,
                           "quarantine_candidates": 0, "fixture_mapping_error": None}],
            "projected_import": {"rejected_lines": 0, "quarantined_lines": 0},
        }
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            lock_handles = []

            class HeldLockPath:
                parent = root

                def open(self, mode):
                    handle = (root / "pipeline.lock").open(mode)
                    lock_handles.append(handle)
                    return handle

            stack.enter_context(patch.multiple(
                pipeline, ROOT=root, RUNS=root / "runs", RESULTS=root / "results",
                LOCK=HeldLockPath(), LATEST_RESULT=root / "latest.json",
            ))
            stack.callback(lambda: [handle.close() for handle in lock_handles])
            stack.enter_context(patch.dict(os.environ, {"BRUCEBET_VK_PIPELINE_ALLOW_IMPORT": "0"}))
            stack.enter_context(patch.object(pipeline, "now", return_value=datetime(2026, 9, 18, tzinfo=timezone.utc)))
            stack.enter_context(patch.object(pipeline, "inspect_production", return_value={"unchanged": True}))
            stack.enter_context(patch.object(pipeline, "copy_source", return_value=(root, {
                "capture_fingerprint": "a" * 64, "run_id": "test", "newest_post_id": 1,
                "newest_visible_timestamp": "18 сен 2026 в 10:13", "displayed_total": 1,
            }, {})))
            stack.enter_context(patch.object(pipeline, "check_fresh_pull", return_value={
                "source_run_id": "test", "source_finished_at": "2026-09-18T00:00:00Z",
            }))
            stack.enter_context(patch.object(pipeline, "backup_database", return_value={"integrity": "ok"}))
            stack.enter_context(patch.object(pipeline, "docker_base", return_value=["fake-docker"]))
            stack.enter_context(patch.object(pipeline, "clean_old_runs"))
            calls = []

            def run_checked(command):
                calls.append(command)
                self.assertIn("/tools/processor.py", command)
                reconciliation = next((root / "runs").iterdir()) / "reconciliation"
                reconciliation.mkdir()
                (reconciliation / "dry-run-result.json").write_text(json.dumps(report), encoding="utf-8")
                return {"returncode": 0}

            stack.enter_context(patch.object(pipeline, "run_checked", side_effect=run_checked))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, pipeline.main())
            self.assertEqual(1, len(calls))
            outcome = json.loads((root / "latest.json").read_text())
            self.assertEqual("new_source_requires_approval", outcome["import_decision"])
            self.assertTrue(outcome["production_unchanged"])

    def test_guarded_import_has_empty_telegram_recipients(self):
        path = OPS / "production" / "guarded_import.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "import_vk_prediction_report"]
        self.assertEqual(1, len(calls))
        recipients = [keyword.value for keyword in calls[0].keywords
                      if keyword.arg == "notification_chat_ids"]
        self.assertEqual(1, len(recipients))
        self.assertIsInstance(recipients[0], ast.Tuple)
        self.assertEqual([], recipients[0].elts)
        self.assertNotIn("deliver_pending_vk_prediction_notifications", path.read_text(encoding="utf-8"))
        unit = (OPS / "systemd" / "production" / "brucebet-vk-pipeline.service").read_text(encoding="utf-8")
        self.assertIn("Environment=BRUCEBET_VK_PIPELINE_ALLOW_IMPORT=0", unit)


if __name__ == "__main__":
    unittest.main()
