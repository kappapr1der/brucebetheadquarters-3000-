# VK integration (public read-only browser probe)

BruceBet remains EPL-only. The VK layer is intended to read the public Forecasters Club discussion topic for EPL and later feed participants/predictions into BruceBet without manual copy/paste.

## What was verified

The target Forecasters Club discussion pages are readable without VK authorization in an incognito browser.

A live server test with headless Chromium successfully rendered a public topic and exposed:

- the discussion title;
- the round template and deadline;
- participant/team labels;
- football fixtures;
- submitted score predictions.

The earlier VK API service-token route was rejected by VK with error `1051` (`Method is not available for this profile type`). The browser route therefore becomes the primary integration path and does **not** require a VK access token.

## Current stage

The Chromium reader and dry-run parser render a public VK topic and extract visible text. They never change VK.

When the explicitly configured EPL registration topic is enabled, a separate monitor writes recognized registration entries to SQLite and creates or updates the active-season participant record. Forecast topics remain read-only until their import workflow is separately enabled.

Chromium is installed in the BruceBet Docker image so the same browser path can later be used by scheduled synchronization.

## Environment

```env
VK_GROUP_ID=217130885
VK_REGISTRATION_TOPIC_ID=
VK_PREDICTIONS_TOPIC_ID=
VK_REGISTRATION_SYNC_ENABLED=0
VK_REGISTRATION_SYNC_INTERVAL_MINUTES=5
VK_REGISTRATION_SYNC_FIRST_DELAY_SECONDS=20
VK_REGISTRATION_BROWSER_WAIT_MS=12000
VK_PREDICTIONS_SNAPSHOT_ENABLED=0
VK_PREDICTIONS_SNAPSHOT_INTERVAL_MINUTES=20
VK_PUBLIC_SNAPSHOT_DIR=data/vk_snapshots
VK_TOPIC_DISCOVERY_ENABLED=1
VK_TOPIC_DISCOVERY_INTERVAL_MINUTES=30
VK_TOPIC_DISCOVERY_FIRST_DELAY_SECONDS=20
VK_CHROMIUM_BIN=chromium
VK_BROWSER_WAIT_MS=12000
```

The topic IDs are deliberately separate: one registration topic and one prediction topic. Set `VK_REGISTRATION_SYNC_ENABLED=1` only after `VK_REGISTRATION_TOPIC_ID` points to the real EPL registration discussion.

No VK token is required for the public-topic reader.

## Automatic topic discovery

Before the two EPL discussions exist, BruceBet can watch the public Forecasters Club discussion list without a VK token:

```bash
python -m brucebet.cli vk-discover
```

The browser tries the public discussion-list page first and then the public group page as a fallback. It recognizes only titles that explicitly identify EPL plus either registration/participants/fee language or prediction language. RPL and other-league topics remain visible in a manual scan but never enter the notification queue.

With `VK_TOPIC_DISCOVERY_ENABLED=1`, the Telegram bot runs the same scan every `VK_TOPIC_DISCOVERY_INTERVAL_MINUTES` and sends one alert to the Telegram whitelist for each newly seen qualifying EPL topic. The first pass that exposes at least one topic link is a quiet baseline: it records already-existing topics and sends no alert. A blank render never establishes that baseline. Use `/vk_topics` for an immediate read-only scan at any time.

Discovery records only its own alert state in SQLite. It does not configure `VK_REGISTRATION_TOPIC_ID` or `VK_PREDICTIONS_TOPIC_ID`, import participants, import forecasts, or make any VK write.

## Probe a topic by URL

```bash
python -m brucebet.vk_board \
  --topic-url https://vk.ru/topic-217130885_66960850 \
  --show-lines 120
```

The RPL topic above is only a temporary public-rendering/format test. Its data must not be imported into BruceBet.

Expected output includes diagnostics such as:

```text
VK group: 217130885
VK topic: 66960850
URL: https://vk.ru/topic-217130885_66960850
HTML chars: ...
Visible chars: ...
Prediction-like score lines: ...
Forecasters Club visible: yes
```

The probe can also preserve diagnostics without touching SQLite:

```bash
python -m brucebet.vk_board \
  --topic-url <VK_TOPIC_URL> \
  --html-out data/vk_topic_debug.html \
  --text-out data/vk_topic_debug.txt
```

## Structured dry-run parser

Use a future EPL topic URL explicitly. The command is read-only and does not need a configured topic ID:

```bash
python -m brucebet.vk_dry_run \
  --kind predictions \
  --topic-url <EPL_PREDICTIONS_TOPIC_URL>

python -m brucebet.vk_dry_run \
  --kind registration \
  --topic-url <EPL_REGISTRATION_TOPIC_URL>
```

For a prediction topic the report recognizes the round template, deadline, VK comment author, declared participant, labelled fixture scores, normalized score form, and whether every fixture was submitted. Duplicate or incomplete blocks remain warnings: they are never guessed or imported.

For a registration topic it reports each declaration separately: VK author, declared participant, fee intent (`paid_declared`, `free`, or `unknown`), declared fee amount, and payment state. In this contest, writing a paid or free status in the registration discussion is the confirmation: `paid_declared` becomes a paid season participant, while `free` becomes a free participant. An unknown fee status is retained without prize eligibility until it is clarified. The parser also detects visible registration-closed and final-roster markers.

Every report contains a content fingerprint and provisional per-comment source key. The registration monitor uses the key for idempotency, updates `last_seen_at` on repeated reads, and sends Telegram notices only for new or changed entries. The source key remains auditable in SQLite.

`/vk_snapshot` reads each configured EPL topic now and reports whether the field changed. Every changed read is stored locally under `VK_PUBLIC_SNAPSHOT_DIR`: it contains the original visible text, the parser result and a content fingerprint. These files never enter the contest SQLite tables or a public GitHub repository. When `VK_PREDICTIONS_SNAPSHOT_ENABLED=1`, the predictions topic receives the same read-only snapshot every `VK_PREDICTIONS_SNAPSHOT_INTERVAL_MINUTES` minutes. It is deliberately not a forecast import.

The dry-run shows `league gate: epl`, `non_epl`, or `unknown`. Non-EPL topics, including the temporary RPL probe topic, may be parsed for format testing but are explicitly not future-ingestion-ready.

## Safety rules

- Public read-only browser rendering only at this stage.
- No form submissions, likes, comments or other VK writes.
- Registration entries are written only when the configured topic passes the EPL gate.
- No predictions are written to SQLite yet.
- No RPL/other-league data is added to the EPL season.
- The future EPL topic will be configured through `VK_PREDICTIONS_TOPIC_ID` or supplied by URL.

## Browser notes

Headless Chromium can emit harmless container warnings about DBus, GCM or GPU/WebGL. The probe captures Chromium stderr and only treats a non-zero browser exit, timeout, missing executable or empty page as a failure.

The important success signals are rendered visible text containing `Forecasters Club` and prediction-like fixture/score lines.

## Next step

After the EPL topic appears:

1. Point `VK_PREDICTIONS_TOPIC_ID` at that EPL topic.
2. Keep the registration and prediction topic monitors separate. The registration monitor polls the configured discussion every five minutes by default and emits one notice per new/changed applicant.
3. Set `VK_PREDICTIONS_SNAPSHOT_ENABLED=1` to archive changed prediction fields every 20 minutes by default, then use `/vk_snapshot` immediately before `/recommend` to capture the final field snapshot.
4. Extract durable VK comment IDs from the rendered DOM, then add idempotent persistence and edit detection.
5. Only after validation, enable scheduled sync into the active EPL season.
