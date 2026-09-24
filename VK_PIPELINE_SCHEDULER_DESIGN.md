# VK Pipeline Scheduler Design

Status: operational design and installed-unit reference. As of 2026-09-24, the
Timeweb Browser Node reader is manually triggered; production pull/pipeline
services are installed, but both timers are disabled/inactive. The pipeline
unit pins `BRUCEBET_VK_PIPELINE_ALLOW_IMPORT=0`. This document does not enable
timers, live import, or Telegram delivery. See `ops/vk/README.md` for the
byte-identical installed sources and deployment boundaries.

## Notification disposition

The 27 events created by the controlled Rounds 3-4 import are historical audit events. They have no rows in `vk_prediction_notification_deliveries`, so the sender cannot select or deliver them. Replaying the same canonical revisions with chat IDs does not create delivery rows because duplicate revisions do not enqueue another event.

Policy:

- keep these 27 event rows as immutable import evidence;
- never synthesize delivery rows for an existing event;
- scheduled imports must pass an empty `notification_chat_ids` collection;
- the scheduler must not invoke `deliver_pending_vk_prediction_notifications`;
- future realtime Telegram notifications, if approved, need a separate explicit policy and migration gate.

## Proposed stages

| Stage | Frequency | Lock | Timeout | Retry | Fail-closed rules | Retention |
| --- | --- | --- | --- | --- | --- | --- |
| Timeweb Browser Node capture | Manual only; no reader timer installed | One host `flock`; never run parallel Chrome | Reader service timeout 240 seconds | No retry in one run | Challenge, incomplete pagination, count mismatch, or cleanup failure cannot publish `latest-complete` | Reader retention drop-in prunes only eligible runs |
| Timeweb SSH pull | Installed timer at `*:07,27,47`, currently disabled | One pull `flock` | Service timeout 90 seconds | No in-run retry | Host-key, schema, SHA-256, fingerprint, topic, completeness, duplicate-ID, or freshness failure cannot replace inbox latest | Pull retention keeps 30 accepted runs and protects latest |
| Inbox validation | Installed timer at `*:09,29,49`, currently disabled | Pipeline lock; production DB backup copy | Service timeout 240 seconds | No automatic retry for content errors | Unknown participant, changed canonical post, quarantine candidate, unsupported schema, stale source, or incomplete capture stops before import | Pipeline run retention |
| Guarded import | Disabled by `ALLOW_IMPORT=0`; separate approval required | SQLite transaction plus pipeline lock | Bounded by pipeline service | No automatic retry after transaction start | Empty notification recipients; rollback on count, history, frozen-pick, integrity, or FK mismatch | Import receipts and table diffs |

## End-to-end gate

1. Capture publishes only a complete sanitized artifact and never overwrites `latest-complete` with an incomplete run.
2. Pull verifies the pinned SSH host key, manifest, schema, SHA-256, fingerprint, topic IDs, canonical uniqueness, and freshness before atomic inbox publication.
3. A known fingerprint is a transport and parser no-op.
4. Validation compares canonical post revisions against SQLite without writes.
5. `new` is eligible for guarded import only when every identity and fixture is known and no quarantine is projected.
6. `changed`, `unknown`, and `quarantine` require a separate operator gate. They are never imported automatically.
7. Guarded import runs in one transaction with `notification_chat_ids=()` and does not call any Telegram sender.
8. Duplicate canonical revisions must produce zero DML and zero `sqlite3.total_changes`.
9. Integrity, FK, protected-history, and frozen-recommendation checks run before commit and again in the receipt verification.

## Failure isolation

- Browser-node failure cannot reach the production host or its credentials.
- Pull failure cannot alter the accepted inbox artifact.
- Parser or validation failure cannot open a write transaction.
- Import failure rolls back the whole candidate and leaves the previous receipt current.
- Logs contain only sanitized IDs, hashes, counts, stop reasons, and bounded errors.
- The no-op fix is deployed, but the timers remain disabled pending a separate
  observation-mode enablement review. Automatic import remains disabled.

## Enablement prerequisites

- duplicate physical no-op fix deployed and verified on a production copy;
- synchronize installed operational scripts and units into Git source of truth;
- separately approve and verify the 13 new Round 5 submissions before any live import;
- preserve the pre-R3/R4 rollback backup through several successful automatic cycles;
- add one end-to-end scheduler rehearsal on a fresh production copy;
- approve timer cadence and operational ownership;
- keep Telegram delivery disabled for scheduled imports.
