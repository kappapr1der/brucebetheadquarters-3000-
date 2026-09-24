# VK Pipeline Scheduler Design

Status: operational design and installed-unit reference. At the 2026-09-24 UTC
observation gate, the Browser Node reader timer was installed from
`main=3e1f0b078446309a4ab0305ec203db379d2f706b` with SHA-256
`a342ccfc068c1f11a8ee61b6476e2bf54260544601f0082f12fddeea208aeaaa`.
Reader, pull, and pipeline timers were enabled and passed three unattended
cycles. The pipeline unit still pins `BRUCEBET_VK_PIPELINE_ALLOW_IMPORT=0`;
automatic live import and scheduled Telegram delivery remain off. This
documentation update changes no server state. See `ops/vk/README.md` for the
installed-source inventory and recovery boundaries.

## Notification disposition

The 27 events created by the controlled Rounds 3-4 import are historical audit events. They have no rows in `vk_prediction_notification_deliveries`, so the sender cannot select or deliver them. Replaying the same canonical revisions with chat IDs does not create delivery rows because duplicate revisions do not enqueue another event.

Policy:

- keep these 27 event rows as immutable import evidence;
- never synthesize delivery rows for an existing event;
- scheduled imports must pass an empty `notification_chat_ids` collection;
- the scheduler must not invoke `deliver_pending_vk_prediction_notifications`;
- future realtime Telegram notifications, if approved, need a separate explicit policy and migration gate.

## Scheduled stages

| Stage | Frequency | Lock | Timeout | Retry | Fail-closed rules | Retention |
| --- | --- | --- | --- | --- | --- | --- |
| Timeweb Browser Node capture | Enabled timer at `*:00,20,40` plus fixed 0-120s delay | One host `flock`; never run parallel Chrome | Reader start timeout 240 seconds, stop timeout 20 seconds | No retry in one run | Challenge, incomplete pagination, count mismatch, or cleanup failure cannot publish `latest-complete` | Reader retention drop-in prunes only eligible runs |
| Timeweb SSH pull | Enabled timer at `*:07,27,47` | One pull `flock` | Pull start timeout 60 seconds, stop timeout 10 seconds | No in-run retry | Host-key, schema, SHA-256, fingerprint, topic, completeness, duplicate-ID, or freshness failure cannot replace inbox latest | Pull retention keeps 30 accepted runs and protects latest |
| Inbox validation | Enabled timer at `*:09,29,49` | Pipeline lock; production DB backup copy | Service timeout 240 seconds | No automatic retry for content errors | Unknown participant, changed canonical post, quarantine candidate, unsupported schema, stale source, or incomplete capture stops before import | Pipeline run retention |
| Guarded import | Disabled by `ALLOW_IMPORT=0`; separate approval required | SQLite transaction plus pipeline lock | Bounded by pipeline service | No automatic retry after transaction start | Empty notification recipients; rollback on count, history, frozen-pick, integrity, or FK mismatch | Import receipts and table diffs |

## End-to-end gate

The installed reader timer uses systemd 255 `FixedRandomDelay=yes` with
`RandomizedDelaySec=120s`, `AccuracySec=1s`, and `Persistent=false`. Both hosts
used UTC and reported synchronized NTP at the observation gate. Even at the latest configured
reader start, the `240s` start and `20s` stop bounds leave at least `39s`
before the `:07/:27/:47` pull. The pull's `60s` start and `10s` stop bounds
leave at least `49s` before `:09/:29/:49` reconciliation. These are configured
bounds, not a guarantee against unbounded host scheduling delays; freshness
and completeness checks remain fail-closed.

Three consecutive unattended cycles on 2026-09-24 UTC completed without
challenge, incomplete capture, transport failure, or Telegram delivery. Each
capture had 71/71 records and the same fingerprint. The source chain was
confirmed by `capture run_id == pull source_run_id == pipeline observed_run_id`
in each cycle. Since each pull was `duplicate_noop=true`, the pipeline's older
`logical_run_id` correctly remained unchanged. The observed minimum gaps were
360 seconds from capture finish to pull start and 117 seconds from pull finish
to pipeline start. Reconciliation returned zero new/changed/unknown/quarantine
and `duplicate_physical_noop` with `total_changes=0` on a DB copy. Production
remained at 648 predictions and 695 revisions during the gate. Freshness alone
is not proof of source-chain correlation.

1. Capture publishes only a complete sanitized artifact and never overwrites `latest-complete` with an incomplete run.
2. Pull verifies the pinned SSH host key, manifest, schema, SHA-256, fingerprint, topic IDs, canonical uniqueness, and freshness before atomic inbox publication.
3. A known fingerprint creates no new logical event, but a fresh complete
   duplicate advances the reader's latest pointer and is still validated for
   source freshness by pull and reconciliation.
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
- The no-op fix is deployed, and timers are enabled only for observation.
  Automatic import and scheduled Telegram delivery remain disabled.

## Automatic-import boundary

- The duplicate physical no-op fix is deployed and verified on a production
  copy; operational sources and the reader timer are in Git and installed.
- Round 5 historical recovery is complete. Preserve the pre-R3/R4 rollback
  backup through several successful automatic cycles.
- Three unchanged-source unattended cycles passed. A real unattended
  `new_source_requires_approval` result still awaits a naturally new forecast.
- Keep `BRUCEBET_VK_PIPELINE_ALLOW_IMPORT=0` and scheduled Telegram delivery
  disabled until a separate approval and import-safety review.
- Treat `round_reviews.completed_at` churn as separate technical debt, not a
  reason to alter this observation-mode scheduler or historical recommendations.
