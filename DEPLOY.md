# Deploy

## Server requirements

- Docker
- Docker Compose plugin
- outbound internet access for Telegram Bot API

## First deploy

Copy the project folder to the server, then:

```bash
cd brucebet-3000
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f brucebet
```

Required env:

- `TELEGRAM_BOT_TOKEN` - token from BotFather.
- `TELEGRAM_ALLOWED_CHAT_IDS` - comma-separated chat ids allowed to use the bot.

Recommended env:

- `BRUCEBET_DB_PATH=data/forecasters.sqlite`
- `BRUCEBET_DATA_DIR=data`
- `BRUCEBET_USER_PARTICIPANT=Bruce Wayne`
- `BRUCEBET_COMPETITION=epl`
- `BRUCEBET_SEASON=2026/27`
- `BRUCEBET_SEASON_DISPLAY=EPL 2026/27`
- `BRUCEBET_LOCK_MINUTES=90`

## Smoke test

In Telegram:

```text
/start
/hq
/deadlines
/table
/audit
/field Arsenal
/recommend Arsenal
/risk
/strategy
/schedule
```

`/schedule` puts reminder jobs in the running process for the current chat. If the container restarts, run `/schedule` again.

## Updating data

Send the VK pasted text as a message or `.txt` file. The bot will parse it, update `data/vk_matches.csv`, `data/vk_predictions.csv`, and import into SQLite.

The `data/` directory is mounted as a Docker volume, so database and parsed CSV files survive container rebuilds.
