# VK integration (read-only probe)

BruceBet remains EPL-only. The VK layer is intended to read Forecasters Club discussion topics and later feed EPL participants/predictions into BruceBet without manual copy/paste.

## Current stage

This branch only adds a **read-only diagnostic probe**. It does not write anything to the BruceBet database and does not modify VK.

The probe uses VK API method `board.getComments` with `extended=1` so it can receive comment text, author IDs and author metadata.

Default VK API version: `5.199`.

## Environment

```env
VK_ACCESS_TOKEN=
VK_API_VERSION=5.199
VK_GROUP_ID=217130885
VK_PREDICTIONS_TOPIC_ID=
VK_SYNC_INTERVAL_MINUTES=5
```

Do not commit a real access token.

## Probe a topic by URL

Once `VK_ACCESS_TOKEN` is configured:

```bash
python -m brucebet.vk_board \
  --topic-url https://vk.ru/topic-217130885_66960850 \
  --limit 100 \
  --show 10
```

The RPL topic above is only a temporary format/connectivity test. Its data must not be imported into BruceBet.

Expected output shape:

```text
VK group: 217130885
VK topic: 66960850
API version: 5.199
Comments in topic: ...
Fetched: ...
Unique authors in fetched slice: ...

Sample comments:
- #... | Name Surname (...) | ... | ...
```

For machine-readable diagnostics:

```bash
python -m brucebet.vk_board --topic-url <VK_TOPIC_URL> --json
```

## Safety rules

- Read-only API calls only at this stage.
- No predictions are written to SQLite yet.
- No RPL/other-league data is added to the EPL season.
- `VK_ACCESS_TOKEN` belongs in deployment secrets / `.env`, never Git.
- The future EPL topic will be configured through `VK_PREDICTIONS_TOPIC_ID` or supplied by URL.

## Next step

After VK authorization is available and the probe successfully reads a public discussion topic:

1. Extract the per-comment prediction parser from the legacy pasted-text `vk_parser.py` flow.
2. Add persistent VK identity mapping (`vk_user_id -> participant_id`).
3. Store processed VK comment IDs so repeated syncs are idempotent.
4. Add dry-run EPL import.
5. Only after validation, enable scheduled sync into the active EPL season.
