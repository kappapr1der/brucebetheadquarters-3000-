# BruceBet Headquarters 3000

Личный штаб прогнозиста для длинного сезона АПЛ.

Задача бота не в том, чтобы притворяться футбольным оракулом. Его задача: держать тур, дедлайн, поле прогнозов, таблицу, риск и стратегию в одном месте, чтобы решение было лучше, чем “ну тут вроде 2:0”.

## Active Profile

По умолчанию проект работает как EPL-штаб:

- competition: `epl`
- season: `2026/27`
- display: `EPL 2026/27`
- пользователь: `Bruce Wayne`
- дедлайн: в старт первого матча тура
- очки: точный счет 3, разница 2, исход 1

Профиль лежит в `configs/epl_2026_27.json`, будущие сезоны можно делать копией `configs/epl_template.json`.

## Что Уже Есть

- SQLite-ядро для сезонов, участников, взносов, туров, матчей, прогнозов и результатов.
- Сезонные взносы: один и тот же участник может играть в разных сезонах с разным статусом оплаты.
- Гибкий парсер счёта: `2:1`, `2-1`, `2—1`, `2;1`, `2 : 1` принимаются и нормализуются.
- Двузначные счета вроде `10:0` считаются невалидными и уходят в аудит.
- Таблица с тай-брейками: очки, точные, разницы, очки последних туров.
- `/hq`: штаб активного тура.
- `/start`: короткий операторский старт: бот готов принять список участников или следующий блок прогнозов.
- `/ready`: предтуровый preflight: дедлайн, покрытие прогнозами, готовность модели и свежесть источников.
- `/missing [тур]`: адресный список участников с неполным блоком и номерами недостающих матчей.
- `/intel [тур]`: готовность аналитики по каждому матчу и точный список того, что ещё нужно проверить.
- `/absence`: быстрая запись подтверждённой травмы/дисквалификации; сразу пересчитывает факторы и оценку модели.
- `/risk`: риск-карта тура.
- `/edge`: карта расхождений поля, рынка и модели; ранжирует матчи для точечных отличий, а не выдаёт «истину».
- `/strategy`: режим игры относительно лидера.
- `/field`, `/recommend`, `/match`, `/vs`, `/audit`, `/deadlines`.
- `/quota`, `/sync_odds`, `/odds`: проверка квоты The Odds API, синк кэфов, просмотр снимков.
- `/sources`: health-check всех подключенных источников данных.
- `/sync_fixtures`: официальный календарь Premier League из public API сайта PL.
- `vk_board`: публичное read-only чтение VK-темы через Chromium; `vk_dry_run` структурирует регистрацию или прогнозы без SQLite.
- `/sync_results`: только финальные результаты из официального PL feed, затем автоматическое закрытие завершённого тура.
- Сервисные сообщения: “принято”, “теперь кидай прогнозы участников”, “проверь аудит”.
- Устойчивые напоминания за 24 часа, 6 часов, 3 часа, 1 час и 20 минут до дедлайна: доставки лежат в SQLite и переживают рестарт контейнера.
- `/review <тур>`: пост-туровый разбор, туровая таблица и матчи-качели.
- `/calibration [тур]`: честная калибровка замороженных до kickoff прогнозов модели.
- `/rehearse`: изолированная репетиция полного тура без изменения живой базы.
- `/setresult`: ручной резервный финальный счёт с неизменяемой историей исправлений.
- `/overrideforecast`: осознанная ручная правка прогноза с аудитом и сохранением исходного времени отправки.
- Docker-деплой Telegram-бота.

## Быстрый Старт

```powershell
python -m brucebet.cli --db brucebet.sqlite load-sample
python -m brucebet.cli --db brucebet.sqlite sync-fixtures
python -m brucebet.cli --db brucebet.sqlite sync-variables
python -m brucebet.cli --db brucebet.sqlite sync-results
python -m brucebet.cli --db brucebet.sqlite review 1
python -m brucebet.cli --db brucebet.sqlite calibration
python -m brucebet.cli rehearse
python -m brucebet.cli --db brucebet.sqlite snapshot --out-dir data/snapshots/current
python -m brucebet.cli --db brucebet.sqlite hq
python -m brucebet.cli --db brucebet.sqlite ready
python -m brucebet.cli --db brucebet.sqlite risk
python -m brucebet.cli --db brucebet.sqlite edge
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
$env:TELEGRAM_ALLOWED_CHAT_IDS="123456789"
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
BRUCEBET_ALLOW_UNRESTRICTED_CHATS=0
BRUCEBET_DB_PATH=data/forecasters.sqlite
BRUCEBET_DATA_DIR=data
BRUCEBET_USER_PARTICIPANT="Bruce Wayne"
BRUCEBET_COMPETITION=epl
BRUCEBET_SEASON=2026/27
BRUCEBET_SEASON_DISPLAY="EPL 2026/27"
# EPL Forecasters Club: submissions close at the first match's kickoff.
BRUCEBET_LOCK_MINUTES=0
BRUCEBET_TIMEZONE=Europe/Moscow
PREMIER_LEAGUE_COMPSEASON_ID=841
PREMIER_LEAGUE_SEASON_LABEL=2026/2027
BRUCEBET_AUTO_SYNC=1
BRUCEBET_AUTO_SYNC_INTERVAL_HOURS=12
BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES=5
BRUCEBET_RESULT_SYNC_INTERVAL_MINUTES=15
BRUCEBET_REMINDER_INTERVAL_MINUTES=5
BRUCEBET_REMINDER_GRACE_MINUTES=35
BRUCEBET_FINAL_PICK_LEAD_MINUTES=10
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
BRUCEBET_AUTO_ODDS_SYNC=1
BRUCEBET_AUTO_ODDS_WINDOW_HOURS=72
BRUCEBET_AUTO_ODDS_CHECK_INTERVAL_MINUTES=15
API_FOOTBALL_KEY=
FOOTBALL_DATA_TOKEN=
THESPORTSDB_KEY=123
```

Команды Telegram:

- `/start`
- `/id`
- `/hq`
- `/ready`
- `/intel [тур]`
- `/absence Arsenal | Saka | doubtful | 0.8 | Arsenal official | ankle knock`
- `/missing [тур]`
- `/load`
- `/participants` + список с новой строки
- `/forecast Имя участника | тур` + счета с новой строки
- `/table`
- `/field <матч>`
- `/recommend <матч>`
- `/picks [тур]`
- `/template [тур]` - чистый русскоязычный блок для вставки в VK
- `/edge [тур]`
- `/odds <матч>`
- `/quota`
- `/sources`
- `/sync_fixtures`
- `/sync_results`
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
- `/review <тур>`
- `/calibration [тур]`
- `/rehearse`
- `/setresult Arsenal - Chelsea | 2:1 | официальный feed задержался`
- `/resulthistory Arsenal`
- `/overrideforecast Igor | Arsenal - Chelsea | 2:1 | подтверждённая опечатка`
- `/forecasthistory Igor | Arsenal - Chelsea`

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
- `brucebet sync-fixtures` - fetch official Premier League fixtures into `matches` using the stable
  `fixture.id` from the public PL API. `position` is presentation order only: kickoff rescheduling may
  move it without changing `match_id` or any attached forecasts. The first migration of an existing
  season requires a complete one-to-one `season + home + away` match and rolls back on missing or
  ambiguous fixtures.
- `brucebet sync-variables` - fetch FPL, ClubElo, context/weather, factors, and draft assessments.
- `brucebet sync-results` - write only completed official results and save completed round reviews.
- `brucebet ready` - preflight the active round: deadline, coverage, model, and data freshness.
- `brucebet missing [тур]` - names and missing match positions for incomplete forecast blocks.
- `brucebet edge [тур]` - rank matches where field consensus, market implied outcome, and model disagree.
- `brucebet import-forecast <участник> <тур> <файл>` - import one participant's raw forecast block.

After the deadline, a normal import can add a previously absent line but cannot replace an already stored score. Use the whitelisted Telegram command `/overrideforecast Участник | Матч | 2:1 | причина` only for an intentional correction; every correction is written to the audit log and exported in snapshots.

Before a score decision, use `/intel [тур]` to see where the model lacks fresh input. Record a confirmed injury or suspension with `/absence Команда | Игрок | статус | impact | источник | заметка`; `impact` uses a scale from `0` to `1`, and `fit`/`available` removes a previous record.
- `brucebet set-result <match> <score> --reason <text>` - manually record a fallback final score with an audit trail.
- `brucebet result-history <match>` - inspect the manual result override journal.
- `brucebet review <тур>` - post-round scoreboard, score swings, and model performance.
- `brucebet calibration [тур]` - accuracy/points for pre-kickoff frozen model forecasts.
- `brucebet rehearse` - isolated end-to-end rehearsal without live database writes.
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

- FPL player availability/form snapshots into `player_status_snapshots`; these are flags, not confirmed medical reports.
- football-data.org completed matches into `team_form`; gaps first fall back to each club's official competition history, then to Championship history for recently promoted clubs.
- ClubElo ratings into `teams.elo_rating`.
- Venue, rest days, weather window notes, and weather when the match is within the Open-Meteo forecast horizon.
- Team match factors: lineup confidence, absences impact, fatigue, baseline motivation.
- Draft `match_assessments` based on Elo and latest stored odds when available.

Telegram has `/sync_variables`, `/sync_results`, and `/dossier <match>`. The bot also runs a quiet background sync every `BRUCEBET_AUTO_SYNC_INTERVAL_HOURS` when `BRUCEBET_AUTO_SYNC=1`, after `BRUCEBET_AUTO_SYNC_FIRST_DELAY_MINUTES` on startup. A separate official-result check runs every `BRUCEBET_RESULT_SYNC_INTERVAL_MINUTES`, closes a fully finished tour, and posts one durable overall table to subscribed chats. Model drafts are frozen only once the relevant tour deadline has arrived; the deadline dispatcher checks this every `BRUCEBET_REMINDER_INTERVAL_MINUTES`. Finished official results are checked without spending Odds API credits.

The routine variable sync does not call The Odds API. A separate quiet odds scheduler checks the next round every `BRUCEBET_AUTO_ODDS_CHECK_INTERVAL_MINUTES` and can spend quota only inside `BRUCEBET_AUTO_ODDS_WINDOW_HOURS` before its effective deadline. It refreshes every 12 hours from 72 to 24 hours out, every 6 hours from 24 to 6, every 2 hours from 6 to 2, and every 30 minutes in the final 2 hours. `/sync_odds` remains available for an immediate manual snapshot. `/schedule` subscribes the current chat to persistent reminders; the dispatcher checks due deliveries every `BRUCEBET_REMINDER_INTERVAL_MINUTES` and retries failed sends inside the configured grace window.

## Preflight And Result Fallback

Before publishing forecasts, use `/ready`. It checks kickoff/deadline coverage, your and the field's submissions, frozen model coverage, and the freshness of FPL, Elo, odds, model, and results data. `/hq` includes the same source-freshness panel.

If the official results feed lags, use the restricted command:

```text
/setresult Arsenal - Chelsea | 2:1 | официальный feed задержался
```

The score is normalized, current standings/reviews are recalculated, and the previous score, time, chat, and reason are retained in the SQLite audit log. Inspect it with `/resulthistory Arsenal`.

## Quick Forecast Import

For a full VK export, use `/load` or send the `.txt` file as before. For one participant's raw block, send this directly to the bot:

```text
/forecast Игорь Григорьев | 1
2:1
Liverpool - Burnley 2 - 0
1;1
2—2
```

The named match is placed into its exact fixture position; unlabeled scores fill the remaining positions in template order. Valid scores are saved immediately. The reply separately reports normalized punctuation, missing positions, duplicates, ambiguous lines, and any scores beyond the end of the tour. The same flow also works without a command when the first line is `Прогноз: Игорь Григорьев | 1`.

After `/start`, the shortest operator flow is even simpler. Send a roster first:

```text
Участники:
Игорь Григорьев 300р
Анна Бухтеева 300р
Стас Ручкин без взноса
```

Then send a forecast with the participant name on the first line and scores below. The active upcoming round is chosen automatically:

```text
Игорь Григорьев
2:1
2 - 0
1;1
```

New names without a payment marker are added outside the prize bank until they are resent with `300р`; this prevents accidental prize eligibility.

## Runtime Data

## VK Dry Run

Будущие темы АПЛ пока не заданы: передавай URL явно. Команда читает только публичную страницу через Chromium, не открывает SQLite и ничего не публикует во VK:

```powershell
python -m brucebet.cli vk-dry-run --kind registration --topic-url <VK_REGISTRATION_TOPIC_URL>
python -m brucebet.cli vk-dry-run --kind predictions --topic-url <VK_PREDICTIONS_TOPIC_URL>
python -m brucebet.cli vk-discover
```

Set `VK_TOPIC_DISCOVERY_ENABLED=1` to poll the public Forecasters Club discussion list. The first pass with at least one discovered topic is a quiet baseline; later newly discovered EPL registration or prediction topics produce one Telegram alert. `/vk_topics` runs the same public, read-only check manually. RPL is ignored by the alert queue and no VK discovery result imports contest data.

`/vk_snapshot` reads the configured EPL registration and prediction topics, keeps a local archive of changed public fields in `data/vk_snapshots/`, and reports the recognized entry/block counts. Set `VK_PREDICTIONS_SNAPSHOT_ENABLED=1` to run the same read-only archive job every 20 minutes. VK is always read-only. SQLite projection is a separate explicit gate: `VK_PREDICTIONS_IMPORT_ENABLED=1` imports only the configured EPL topic, only registered participants, and maps fixtures by stable home/away identity rather than VK list position.

VK forecast imports append immutable `prediction_revisions`. Repeating the same capture is a no-op, a changed comment becomes one revision, and an edit first observed after the deadline is rejected without changing the current projection. Ambiguous comment identities, unknown participants, and incomplete fixture mappings go to `vk_prediction_quarantine` rather than being guessed.

When the explicit import gate is enabled, every new meaningful VK forecast event also enters a durable SQLite outbox. Telegram delivery is tracked per whitelisted chat ID: a failed chat stays pending for the next poll, while successful chats are never sent the same event twice. The sanitized operational snapshot includes the event and delivery ledgers, but never Telegram tokens.

## Contest Pick Ledger

`match_assessments` remains an independent football assessment. It never reads participant forecasts and remains suitable for post-season calibration. Bruce's actual contest recommendation is a separate append-only `contest_recommendations` ledger, exposed through `/picks [тур]`; it never inserts or changes Bruce's row in `predictions`. `/template [тур]` renders the same ledger as a Russian copy-ready VK block, while fixture identities remain canonical English names internally.

For each match the deterministic synthesis combines available sources at 55% independent model, 25% market implied probability, and 20% eligible competitor field, normalized when a source is absent. Bruce is excluded from the field. The current standings strategy only applies a bounded adjustment, while the exact score normally stays with the assessment unless a strong field consensus or high volatility triggers the documented fallback. Every input fingerprint, source snapshot, strategy, readiness warning and predecessor record is retained.

Only accepted VK projections for a specific round trigger a field recomputation. Repeated snapshots, quarantined blocks and rejected late edits do not change the contest pick. A durable Telegram delta is queued only when a displayed score/status changes. The final dispatcher checks every minute and freezes a pre-deadline snapshot at `BRUCEBET_FINAL_PICK_LEAD_MINUTES` before the effective deadline (10 minutes by default). A snapshot with incomplete field or intelligence remains explicitly provisional rather than being labelled final.

Для прогнозов выводятся шаблон, дедлайн, автор комментария, фактический участник, нормализованные счета и статус `FULL`/`PARTIAL`. Для регистрации отдельно сохраняется заявленный выбор взноса и статус проверки оплаты: перевод считается только заявленным, пока организатор его не подтвердил. Тестовая тема РПЛ допускается лишь для проверки формата: dry-run пометит её как `non_epl`, а будущий импорт останется заблокированным.

Реальные прогнозы, участники, SQLite и выгрузки должны жить только в серверном `data/`.

В публичный GitHub они не коммитятся: там остаются `data/README.md` и `data/.gitkeep`.

## Runtime Snapshots

Use `brucebet snapshot` to export stable CSV files and `manifest.json` for the active season:

```powershell
python -m brucebet.cli --db brucebet.sqlite snapshot --out-dir data/snapshots/current
```

On the server, `scripts/autocommit-snapshot.sh` commits those exports in a separate git repository at `/opt/brucebet-3000/data/snapshots`. Keep automatic push pointed at a private remote only.

Before the first stable-fixture migration in production, create a consistent SQLite online backup and
verify a restore copy. A successful fixture sync records created/updated/moved/unmatched counts,
before/after fixture hashes, and stale-factor cleanup in `fixture_sync_runs`.

Every forecast ingest also appends a row to `prediction_revisions`. The current `predictions` table
is only the projection of the latest eligible revision. Replays with the same stable source item and
content fingerprint are no-ops; invalid or timezone-naive external timestamps and late edits remain
auditable but cannot change the current score. The calculated round deadline (first kickoff minus
`BRUCEBET_LOCK_MINUTES`) is authoritative for edits; the active EPL profile uses `0`, so it closes
at the first kickoff. The stored round deadline is its fallback. A
participant's first forecast for a later match may still be accepted until that match kicks off;
matches already in progress are excluded.

## World Cup Legacy

Старый ЧМ-сценарий не удалён из архитектуры: VK-парсер и `configs/world_cup_2026.json` оставлены как совместимый режим. Но активная разработка теперь идёт под EPL-longterm: сезонность, профили участников, риск-карта, стратегия и пост-туровый разбор.



