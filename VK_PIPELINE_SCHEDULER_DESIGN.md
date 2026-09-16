# VK Pipeline Scheduler Design

Status: design only. No timers, services, production configuration, Telegram delivery, or deployment are enabled by this document.

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
| IHC capture | Every 20 minutes, with 0-120 second deterministic jitter | One host `flock`; never run parallel Chrome | 8 minutes total, 90 seconds per page action | No retry in one run; next scheduled window only | Challenge, incomplete pagination, count mismatch, or cleanup failure cannot publish `latest-complete` | 14 daily complete artifacts plus 30 days of manifests and failure metadata |
| Timeweb SSH pull | Five minutes after the capture window | One pull `flock` | 90 seconds | One retry after 60 seconds for transport failure only | Host-key, schema, SHA-256, fingerprint, topic, completeness, duplicate-ID, or freshness failure cannot replace inbox latest | 30 accepted immutable artifacts; 30 days of transport logs |
| Inbox validation | Immediately after a newly accepted fingerprint | Same pipeline lock; read-only DB connection | 2 minutes | No automatic retry for content errors | Unknown participant, changed canonical post, quarantine candidate, unsupported schema, stale source, or incomplete capture stops before import | Verification reports for 90 days |
| Guarded import | Only after validation reports a new, clean candidate | SQLite `BEGIN IMMEDIATE` plus pipeline lock | 2 minutes | No automatic retry after transaction start; operator review on ambiguity | Import only a complete allowlisted source; empty notification recipients; rollback on any count, history, frozen-pick, integrity, or FK mismatch | Import receipts and table diffs for the season |

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

- Browser-node failure cannot reach Timeweb or production credentials.
- Pull failure cannot alter the accepted inbox artifact.
- Parser or validation failure cannot open a write transaction.
- Import failure rolls back the whole candidate and leaves the previous receipt current.
- Logs contain only sanitized IDs, hashes, counts, stop reasons, and bounded errors.
- The scheduler remains disabled until the no-op fix is deployed and a separate enablement review approves exact service/timer units and notification policy.

## Enablement prerequisites

- deploy the duplicate physical no-op fix with its regression tests;
- preserve the pre-R3/R4 rollback backup through several successful automatic cycles;
- add one end-to-end scheduler rehearsal on a fresh production copy;
- approve timer cadence and operational ownership;
- keep Telegram delivery disabled for scheduled imports.
