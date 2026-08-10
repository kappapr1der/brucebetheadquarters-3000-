# VK OAuth For Two Fixed EPL Topics

BruceBet reads only the configured EPL registration and prediction discussion
IDs. It never posts to VK. The free Cloudflare Worker temporarily stores only a
short-lived authorization code; the VPS alone exchanges that code and stores the
VK access token in `data/vk_oauth_credentials.json` with mode `0600`.

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

Keep `VK_OAUTH_CLIENT_ID` and `VK_OAUTH_CLIENT_SECRET` only on the VPS. In the
VK application settings, set the exact same value of `VK_OAUTH_REDIRECT_URI` as
the trusted Redirect URL.

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
