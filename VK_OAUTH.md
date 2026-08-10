# VK OAuth For Two Fixed EPL Topics

BruceBet reads only the configured registration and prediction discussion IDs.
It never posts to VK. The authorization code is returned to the VPS callback;
neither the code nor the access token is copied through Telegram or Git.

## One-Time Server Setup

1. In VK application `54715552`, configure the redirect URI:
   `https://8412675-hk474154.twc1.net/vk/oauth/callback`
2. Put the application's secure key directly in `/opt/brucebet-3000/.env` as
   `VK_OAUTH_CLIENT_SECRET`. Keep `VK_OAUTH_CLIENT_ID=54715552` and the redirect
   URI from `.env.example`.
3. On the VPS, replace the old static HTTP server with the Caddy profile while
   preserving its static directory:

   ```bash
   sudo systemctl stop jl105-clan-site
   cd /opt/brucebet-3000
   docker compose --profile oauth up -d --build
   ```

   Caddy serves `/opt/jl105-clan-site` as before and handles HTTPS plus only
   `/vk/oauth/*` for the private callback.
4. Confirm that `https://8412675-hk474154.twc1.net/vk/oauth/healthz` returns
   a small `ok` page.
5. In the allowed Telegram chat, run `/vk_connect`, open the generated link,
   and confirm access in VK. The link expires after 15 minutes.
6. Run `/vk_status`. It must report that both configured EPL topics are readable.

## Safety And Rollback

- `data/vk_oauth_credentials.json` is created with mode `0600`, is ignored by
  Git through the data directory, and is never written to Telegram responses.
- The old `VK_USER_ACCESS_TOKEN` should be removed from `.env`: it is bound to
  the browser IP and is not used by BruceBet.
- If the HTTPS transition needs rollback, run:

  ```bash
  cd /opt/brucebet-3000
  docker compose --profile oauth down
  sudo systemctl start jl105-clan-site
  ```

- A successful OAuth connection does not prove that VK permits `board.getComments`
  for the account. `/vk_status` is the read-only gate; BruceBet keeps its public
  Chromium reader as a fallback if the API denies a request.
