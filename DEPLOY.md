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
- `THE_ODDS_API_KEY` - The Odds API key for `/quota` and `/sync_odds`.
- `API_FOOTBALL_KEY` - API-Football key for football-data source checks and future syncs.
- `FOOTBALL_DATA_TOKEN` - football-data.org token.

Recommended env:

- `BRUCEBET_DB_PATH=data/forecasters.sqlite`
- `BRUCEBET_DATA_DIR=data`
- `BRUCEBET_USER_PARTICIPANT="Bruce Wayne"`
- `BRUCEBET_COMPETITION=epl`
- `BRUCEBET_SEASON=2026/27`
- `BRUCEBET_SEASON_DISPLAY="EPL 2026/27"`
- `BRUCEBET_LOCK_MINUTES=90`
- `BRUCEBET_TIMEZONE=Europe/Moscow`
- `PREMIER_LEAGUE_COMPSEASON_ID=841`
- `PREMIER_LEAGUE_SEASON_LABEL=2026/2027`
- `BRUCEBET_AUTO_SYNC=1`
- `BRUCEBET_AUTO_SYNC_INTERVAL_HOURS=12`
- `BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES=5`
- `BRUCEBET_VARIABLE_DAYS_AHEAD=365`
- `BRUCEBET_WEATHER_DAYS_AHEAD=16`
- `THE_ODDS_API_SPORT=soccer_epl`
- `THE_ODDS_API_REGIONS=eu`
- `THE_ODDS_API_MARKETS=h2h,totals`
- `THE_ODDS_API_BOOKMAKER=market_avg`
- `THE_ODDS_API_DAYS_AHEAD=30`
- `THESPORTSDB_KEY=123`

## Smoke test

In Telegram:

```text
/start
/id
/hq
/calendar
/next
/variables
/quota
/sources
/sync_fixtures
/sync_variables
/sync_odds
/dossier Arsenal
/odds Arsenal
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
