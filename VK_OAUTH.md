# VK OAuth For Two Fixed EPL Topics

BruceBet reads only the configured EPL registration and prediction discussion
IDs. It never posts to VK. The free Cloudflare Worker temporarily stores only a
short-lived authorization code plus VK ID's `device_id`; the VPS alone exchanges
them with PKCE and stores the VK access token in
`data/vk_oauth_credentials.json` with mode `0600`.

## Choosing The VK Application

The modern **Site** / VK ID application signs a person in, but its `vk2.a.*`
token is not accepted by `board.getComments` and returns VK error `1051`.
For the two discussion topics, create a **Standalone application** in the VK
developer panel instead. BruceBet supports both flows:

- `VK_OAUTH_PROVIDER=vk_id` is retained for VK ID sign-in and uses PKCE.
- `VK_OAUTH_PROVIDER=legacy` uses the classic VK API authorization-code flow.
  It requires the Standalone application's ID and secure key, and requests the
  `groups` scope needed for the read-only board API.

## Cloudflare Worker Setup

Use the existing Worker URL, for example
`https://brucebet-vk-oauth.<account>.workers.dev`.

1. In **Cloudflare Dashboard -> Storage & Databases -> KV**, create a namespace
   named `brucebet-vk-oauth-codes`.
2. Open the `brucebet-vk-oauth` Worker. In **Settings -> Bindings**, add a KV
   namespace binding named `OAUTH_CODES` and select that namespace.
3. In **Settings -> Variables and Secrets**, add a secret text variable named
   `RELAY_SECRET`. Its value must match `VK_OAUTH_WORKER_RELAY_SECRET` on the
   VPS. Use at least 24 random characters.
4. Open **Edit code**, replace the default Hello World code with
   `deploy/cloudflare-worker/brucebet-vk-oauth.js`, and deploy it.
5. Confirm that `<worker URL>/healthz` answers with
   `BruceBet VK OAuth Worker relay is ready.`

The Worker has no VK client secret and never receives a long-lived VK token.
The `OAUTH_CODES` KV records expire after 15 minutes.

## Server And VK Setup

In `/opt/brucebet-3000/.env`, set:

```dotenv
VK_OAUTH_WORKER_URL=https://brucebet-vk-oauth.<account>.workers.dev
VK_OAUTH_REDIRECT_URI=https://brucebet-vk-oauth.<account>.workers.dev/vk/oauth/callback
VK_OAUTH_WORKER_RELAY_SECRET=<same random value as the Worker RELAY_SECRET>
VK_OAUTH_WORKER_POLL_INTERVAL_SECONDS=15
```

For a Standalone application, also set:

```dotenv
VK_OAUTH_PROVIDER=legacy
VK_OAUTH_CLIENT_ID=<Standalone application ID>
VK_OAUTH_CLIENT_SECRET=<Standalone secure key>
VK_OAUTH_LEGACY_SCOPE=groups
```

In the VK application settings, set the exact same value of
`VK_OAUTH_REDIRECT_URI` as the trusted Redirect URL.

Restart only BruceBet:

```bash
cd /opt/brucebet-3000
docker compose up -d --build brucebet
```

Then run `/vk_connect` in the allowed Telegram chat and approve access in VK.
The bot polls the Worker every 15 seconds, finishes the server-side exchange,
and sends a confirmation. Use `/vk_status` as the final read-only gate for the
two fixed EPL topics.

## Safety And Rollback

- Do not paste a VK token in Telegram, Git, a browser address bar, or the
  Worker code. Remove old `VK_USER_ACCESS_TOKEN` values from `.env`.
- A successful OAuth connection does not prove that VK permits
  `board.getComments`. `/vk_status` is the gate; public Chromium reading remains
  available as fallback.
- To disable the free callback flow, clear `VK_OAUTH_WORKER_URL` and
  `VK_OAUTH_WORKER_RELAY_SECRET` from `.env`, then restart `brucebet`.
