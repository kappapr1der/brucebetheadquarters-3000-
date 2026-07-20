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
- `BRUCEBET_REMINDER_INTERVAL_MINUTES=5`
- `BRUCEBET_REMINDER_GRACE_MINUTES=35`
- `BRUCEBET_VARIABLE_DAYS_AHEAD=365`
- `BRUCEBET_WEATHER_DAYS_AHEAD=16`
- `BRUCEBET_SNAPSHOT_LABEL=server-auto`
- `BRUCEBET_SNAPSHOT_OUT_DIR=data/snapshots/current`
- `BRUCEBET_SNAPSHOT_REPO=/opt/brucebet-3000/data/snapshots`
- `BRUCEBET_SNAPSHOT_PUSH=auto`
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
/ready
/calendar
/next
/variables
/quota
/sources
/sync_fixtures
/sync_results
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
/review 1
/calibration
/rehearse
/setresult Arsenal - Chelsea | 2:1 | official feed delay
/resulthistory Arsenal
```

`/schedule` subscribes the current chat to persistent deadline reminders. Delivery state is stored in SQLite, so a container restart does not lose the schedule or resend reminders already delivered.

`/ready` is the operational preflight before a round. It surfaces missing forecasts, unknown kickoff times, incomplete model coverage, and source freshness. `/setresult <match> | <score> | <reason>` is restricted by the Telegram whitelist and writes every manual fallback into an audit log; use it only while the official result feed is delayed or being corrected.

## Updating data

Send the VK pasted text as a message or `.txt` file. The bot will parse it, update `data/vk_matches.csv`, `data/vk_predictions.csv`, and import into SQLite.

The `data/` directory is mounted as a Docker volume, so database and parsed CSV files survive container rebuilds.

## Server snapshot autocommits

The production folder itself does not need to be a git repository. Runtime snapshots live in a separate repo under:

```bash
/opt/brucebet-3000/data/snapshots
```

Manual run:

```bash
cd /opt/brucebet-3000
chmod +x scripts/autocommit-snapshot.sh
BRUCEBET_SNAPSHOT_PUSH=0 scripts/autocommit-snapshot.sh
```

The script exports safe CSV/JSON files to `data/snapshots/current`, commits only `.gitignore`, `README.md`, and `current/`, and never commits `.env` or SQLite files.

The host script does not source `.env` by default because Docker env files can contain values that are not shell-safe. Pass snapshot overrides directly before the command when needed.

Cron example, every 6 hours:

```bash
(crontab -l 2>/dev/null; echo '17 */6 * * * cd /opt/brucebet-3000 && BRUCEBET_SNAPSHOT_PUSH=auto scripts/autocommit-snapshot.sh >> /var/log/brucebet-snapshot.log 2>&1') | crontab -
```

Automatic push is optional. Use only a private repository:

```bash
git -C /opt/brucebet-3000/data/snapshots remote add origin git@github.com:kappapr1der/brucebet-private-snapshots.git
git -C /opt/brucebet-3000/data/snapshots push -u origin main
```
