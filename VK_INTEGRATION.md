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

This branch adds a **read-only Chromium probe** and a structured dry-run parser. It renders a public VK topic, extracts visible text and reports what was recognized. The parser never opens SQLite and never changes VK.

It does not write anything to the BruceBet database and does not modify VK.

Chromium is installed in the BruceBet Docker image so the same browser path can later be used by scheduled synchronization.

## Environment

```env
VK_GROUP_ID=217130885
VK_REGISTRATION_TOPIC_ID=
VK_PREDICTIONS_TOPIC_ID=
VK_SYNC_INTERVAL_MINUTES=5
VK_CHROMIUM_BIN=chromium
VK_BROWSER_WAIT_MS=8000
```

Both topic IDs stay empty until the two EPL 2026/27 discussions exist. They are deliberately separate: one registration topic and one prediction topic.

No VK token is required for the public-topic reader.

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

For a registration topic it reports each declaration separately: VK author, declared participant, fee intent (`paid_declared`, `free`, or `unknown`), and an intentionally separate payment state. A claimed payment is always `unverified` until an organizer confirms it in a later workflow. It also detects visible registration-closed and final-roster markers without treating them as database state.

Every report contains a content fingerprint and provisional per-comment source key. The future persistent monitor will use these to compare snapshots. Before any SQLite import it must upgrade to durable VK DOM comment IDs when the rendered topic exposes them; a line-based key alone is not sufficient for a final idempotent importer.

The dry-run shows `league gate: epl`, `non_epl`, or `unknown`. Non-EPL topics, including the temporary RPL probe topic, may be parsed for format testing but are explicitly not future-ingestion-ready.

## Safety rules

- Public read-only browser rendering only at this stage.
- No form submissions, likes, comments or other VK writes.
- No predictions are written to SQLite yet.
- No RPL/other-league data is added to the EPL season.
- The future EPL topic will be configured through `VK_PREDICTIONS_TOPIC_ID` or supplied by URL.

## Browser notes

Headless Chromium can emit harmless container warnings about DBus, GCM or GPU/WebGL. The probe captures Chromium stderr and only treats a non-zero browser exit, timeout, missing executable or empty page as a failure.

The important success signals are rendered visible text containing `Forecasters Club` and prediction-like fixture/score lines.

## Next step

After the EPL topic appears:

1. Point `VK_PREDICTIONS_TOPIC_ID` at that EPL topic.
2. Keep the registration and prediction topic monitors separate. The registration monitor records declarations, closure markers and a final-roster candidate; paid declarations remain unverified until explicitly confirmed.
3. Poll the prediction topic frequently before the round deadline, save immutable parsed snapshots, and capture one final field snapshot immediately before `/recommend` evaluates a match.
4. Extract durable VK comment IDs from the rendered DOM, then add idempotent persistence and edit detection.
5. Only after validation, enable scheduled sync into the active EPL season.
