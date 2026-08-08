# VK integration (read-only probe)

BruceBet remains EPL-only. The VK layer is intended to read Forecasters Club discussion topics and later feed EPL participants/predictions into BruceBet without manual copy/paste.

## Current stage

This branch only adds a **read-only diagnostic probe**. It does not write anything to the BruceBet database and does not modify VK.

The probe uses VK API method `board.getComments` with `extended=1` so it can receive comment text, author IDs and author metadata.

Default VK API version: `5.199`.

According to the official VK API 5.199 schema, both `board.getComments` and `board.getTopics` accept `user` and `service` access-token types. For BruceBet the preferred first attempt is therefore an application's **service access token**, not a community-admin token and not a personal-user OAuth token.

## Environment

```env
VK_ACCESS_TOKEN=
VK_API_VERSION=5.199
VK_GROUP_ID=217130885
VK_PREDICTIONS_TOPIC_ID=
VK_SYNC_INTERVAL_MINUTES=5
```

Do not commit a real access token.

## Preferred token path

1. Create a VK application in the VK ID developer/business console.
2. Open the application settings.
3. If a **service access key/token** is available, use that as `VK_ACCESS_TOKEN` for the read-only probe.
4. Do not request administrator access to Forecasters Club: the target discussion is public and BruceBet only needs read access.
5. If VK rejects the service token for the real topic, capture the exact API error first; only then fall back to a user OAuth token.

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

After a service/user token successfully reads a public discussion topic:

1. Extract the per-comment prediction parser from the legacy pasted-text `vk_parser.py` flow.
2. Add persistent VK identity mapping (`vk_user_id -> participant_id`).
3. Store processed VK comment IDs so repeated syncs are idempotent.
4. Add dry-run EPL import.
5. Only after validation, enable scheduled sync into the active EPL season.
