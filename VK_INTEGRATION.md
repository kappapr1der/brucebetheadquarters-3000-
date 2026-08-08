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

This branch adds a **read-only Chromium probe**. It renders a public VK topic, extracts visible text and reports how many prediction-like score lines were found.

It does not write anything to the BruceBet database and does not modify VK.

Chromium is installed in the BruceBet Docker image so the same browser path can later be used by scheduled synchronization.

## Environment

```env
VK_GROUP_ID=217130885
VK_PREDICTIONS_TOPIC_ID=
VK_SYNC_INTERVAL_MINUTES=5
VK_CHROMIUM_BIN=chromium
VK_BROWSER_WAIT_MS=8000
```

`VK_PREDICTIONS_TOPIC_ID` stays empty until the EPL 2026/27 topic exists.

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
2. Parse participant prediction blocks from the rendered topic text/DOM against the active EPL round fixtures.
3. Add persistent source identifiers so repeated syncs are idempotent and edits can be detected.
4. Add a dry-run EPL import report.
5. Only after validation, enable scheduled sync into the active EPL season.
6. Add the separate contribution/payment-topic reader with an explicit status flow instead of treating newly discovered users as automatically paid.
