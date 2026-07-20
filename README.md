# BruceBet Headquarters 3000

Личный штаб прогнозиста для длинного сезона АПЛ.

Задача бота не в том, чтобы притворяться футбольным оракулом. Его задача: держать тур, дедлайн, поле прогнозов, таблицу, риск и стратегию в одном месте, чтобы решение было лучше, чем “ну тут вроде 2:0”.

## Active Profile

По умолчанию проект работает как EPL-штаб:

- competition: `epl`
- season: `2026/27`
- display: `EPL 2026/27`
- пользователь: `Bruce Wayne`
- дедлайн: за 90 минут до первого матча тура
- очки: точный счет 3, разница 2, исход 1

Профиль лежит в `configs/epl_2026_27.json`, будущие сезоны можно делать копией `configs/epl_template.json`.

## Что Уже Есть

- SQLite-ядро для сезонов, участников, взносов, туров, матчей, прогнозов и результатов.
- Сезонные взносы: один и тот же участник может играть в разных сезонах с разным статусом оплаты.
- Гибкий парсер счёта: `2:1`, `2-1`, `2;1`, `2 : 1` принимаются и нормализуются.
- Двузначные счета вроде `10:0` считаются невалидными и уходят в аудит.
- Таблица с тай-брейками: очки, точные, разницы, очки последних туров.
- `/hq`: штаб активного тура.
- `/risk`: риск-карта тура.
- `/strategy`: режим игры относительно лидера.
- `/field`, `/recommend`, `/match`, `/vs`, `/audit`, `/deadlines`.
- `/quota`, `/sync_odds`, `/odds`: проверка квоты The Odds API, синк кэфов, просмотр снимков.
- `/sources`: health-check всех подключенных источников данных.
- `/sync_fixtures`: официальный календарь Premier League из public API сайта PL.
- Сервисные сообщения: “принято”, “теперь кидай прогнозы участников”, “проверь аудит”.
- Напоминания за 24 часа, 6 часов, 3 часа, 1 час и 20 минут до дедлайна.
- Docker-деплой Telegram-бота.

## Быстрый Старт

```powershell
python -m brucebet.cli --db brucebet.sqlite load-sample
python -m brucebet.cli --db brucebet.sqlite sync-fixtures
python -m brucebet.cli --db brucebet.sqlite sync-variables
python -m brucebet.cli --db brucebet.sqlite snapshot --out-dir data/snapshots/current
python -m brucebet.cli --db brucebet.sqlite hq
python -m brucebet.cli --db brucebet.sqlite risk
python -m brucebet.cli --db brucebet.sqlite strategy
python -m brucebet.cli --db brucebet.sqlite calendar
python -m brucebet.cli --db brucebet.sqlite next
python -m brucebet.cli --db brucebet.sqlite variables Arsenal
python -m brucebet.cli --db brucebet.sqlite dossier Arsenal
python -m brucebet.cli --db brucebet.sqlite odds Arsenal
python -m brucebet.cli --db brucebet.sqlite table
python -m brucebet.cli --db brucebet.sqlite field Arsenal
python -m brucebet.cli --db brucebet.sqlite recommend Arsenal
python -m brucebet.cli --db brucebet.sqlite audit
```

Если `python` не находится в Windows-среде Codex:

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m brucebet.cli --db brucebet.sqlite load-sample
```

## Telegram

Локально:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
python -m brucebet.telegram_app
```

Docker:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f brucebet
```

Основные env:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=
BRUCEBET_DB_PATH=data/forecasters.sqlite
BRUCEBET_DATA_DIR=data
BRUCEBET_USER_PARTICIPANT="Bruce Wayne"
BRUCEBET_COMPETITION=epl
BRUCEBET_SEASON=2026/27
BRUCEBET_SEASON_DISPLAY="EPL 2026/27"
BRUCEBET_LOCK_MINUTES=90
BRUCEBET_TIMEZONE=Europe/Moscow
PREMIER_LEAGUE_COMPSEASON_ID=841
PREMIER_LEAGUE_SEASON_LABEL=2026/2027
BRUCEBET_AUTO_SYNC=1
BRUCEBET_AUTO_SYNC_INTERVAL_HOURS=12
BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES=5
BRUCEBET_VARIABLE_DAYS_AHEAD=365
BRUCEBET_WEATHER_DAYS_AHEAD=16
BRUCEBET_SNAPSHOT_LABEL=server-auto
BRUCEBET_SNAPSHOT_OUT_DIR=data/snapshots/current
BRUCEBET_SNAPSHOT_REPO=/opt/brucebet-3000/data/snapshots
BRUCEBET_SNAPSHOT_PUSH=auto
THE_ODDS_API_KEY=
THE_ODDS_API_SPORT=soccer_epl
THE_ODDS_API_REGIONS=eu
THE_ODDS_API_MARKETS=h2h,totals
THE_ODDS_API_BOOKMAKER=market_avg
THE_ODDS_API_DAYS_AHEAD=30
API_FOOTBALL_KEY=
FOOTBALL_DATA_TOKEN=
THESPORTSDB_KEY=123
```

Команды Telegram:

- `/start`
- `/id`
- `/hq`
- `/load`
- `/table`
- `/field <матч>`
- `/recommend <матч>`
- `/odds <матч>`
- `/quota`
- `/sources`
- `/sync_fixtures`
- `/sync_variables`
- `/sync_odds`
- `/dossier <match>`
- `/risk [тур]`
- `/strategy`
- `/match <матч>`
- `/vs <участник>`
- `/deadlines`
- `/schedule`
- `/audit`

## CSV

Шаблоны лежат в `examples/`:

- `participants.csv` - участники активного сезона и статус взноса.
- `matches.csv` - matchweek, порядок матчей, kickoff, результат.
- `predictions.csv` - прогнозы участников.
- `teams.csv` - сила клубов, стиль, условные рейтинги.
- `team_form.csv` - форма, xG, последние матчи.
- `absences.csv` - травмы, дисквалификации, сомнительные игроки.
- `match_contexts.csv` - стадион, отдых, переезд, погода, мотивация, ротация.
- `match_odds.csv` - снимки коэффициентов.
- `team_match_factors.csv` - матчевые факторы по каждой команде.
- `match_assessments.csv` - ручная оценка штаба: базовый счёт, риск, контр-сценарий.

Additional EPL operator files:

- `player_statuses.csv` - player availability, form rating, minutes/starts/goals/assists/xG/xA snapshots.

Calendar commands:

- `brucebet calendar` - upcoming matches and deadlines.
- `brucebet today` - matches today.
- `brucebet week` - next seven days.
- `brucebet next` - next scheduled match.
- `brucebet round <matchweek>` - one round calendar.
- `brucebet variables [team]` - latest player status/form snapshots.
- `brucebet sync-fixtures` - fetch official Premier League fixtures into `matches`.
- `brucebet sync-variables` - fetch FPL, ClubElo, context/weather, factors, and draft assessments.
- `brucebet snapshot` - export stable sanitized CSV/JSON files for server-side git snapshots.
- `brucebet dossier <team>` - show the match variable card.
- `brucebet quota` - check The Odds API key and remaining credits without spending odds quota.
- `brucebet sources` - check all configured/free data sources.
- `brucebet sync-odds` - fetch EPL odds into `match_odds`.
- `brucebet odds <team>` - show stored odds snapshots for a match.

Импорт:

```powershell
python -m brucebet.cli --db brucebet.sqlite import --reset `
  --participants participants.csv `
  --teams teams.csv `
  --matches matches.csv `
  --predictions predictions.csv `
  --team-form team_form.csv `
  --absences absences.csv `
  --player-statuses player_statuses.csv `
  --contexts match_contexts.csv `
  --odds match_odds.csv `
  --factors team_match_factors.csv `
  --assessments match_assessments.csv
```

## Automated Variables

`sync-variables` fills the first automated analytics layer:

- FPL player availability/form snapshots into `player_status_snapshots`.
- ClubElo ratings into `teams.elo_rating`.
- Venue, rest days, weather window notes, and weather when the match is within the Open-Meteo forecast horizon.
- Team match factors: lineup confidence, absences impact, fatigue, baseline motivation.
- Draft `match_assessments` based on Elo and latest stored odds when available.

Telegram has `/sync_variables` and `/dossier <match>`. The bot also runs a quiet background sync every `BRUCEBET_AUTO_SYNC_INTERVAL_HOURS` when `BRUCEBET_AUTO_SYNC=1`, after `BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES` on startup.

The background sync does not call The Odds API, so it does not spend odds credits. Use `/sync_odds` manually closer to deadline.

## Runtime Data

Реальные прогнозы, участники, SQLite и выгрузки должны жить только в серверном `data/`.

В публичный GitHub они не коммитятся: там остаются `data/README.md` и `data/.gitkeep`.

## Runtime Snapshots

Use `brucebet snapshot` to export stable CSV files and `manifest.json` for the active season:

```powershell
python -m brucebet.cli --db brucebet.sqlite snapshot --out-dir data/snapshots/current
```

On the server, `scripts/autocommit-snapshot.sh` commits those exports in a separate git repository at `/opt/brucebet-3000/data/snapshots`. Keep automatic push pointed at a private remote only.

## World Cup Legacy

Старый ЧМ-сценарий не удалён из архитектуры: VK-парсер и `configs/world_cup_2026.json` оставлены как совместимый режим. Но активная разработка теперь идёт под EPL-longterm: сезонность, профили участников, риск-карта, стратегия и пост-туровый разбор.
