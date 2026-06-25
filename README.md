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
- Сервисные сообщения: “принято”, “теперь кидай прогнозы участников”, “проверь аудит”.
- Напоминания за 24 часа, 6 часов, 3 часа, 1 час и 20 минут до дедлайна.
- Docker-деплой Telegram-бота.

## Быстрый Старт

```powershell
python -m brucebet.cli --db brucebet.sqlite load-sample
python -m brucebet.cli --db brucebet.sqlite hq
python -m brucebet.cli --db brucebet.sqlite risk
python -m brucebet.cli --db brucebet.sqlite strategy
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
BRUCEBET_USER_PARTICIPANT=Bruce Wayne
BRUCEBET_COMPETITION=epl
BRUCEBET_SEASON=2026/27
BRUCEBET_SEASON_DISPLAY=EPL 2026/27
BRUCEBET_LOCK_MINUTES=90
```

Команды Telegram:

- `/start`
- `/hq`
- `/load`
- `/table`
- `/field <матч>`
- `/recommend <матч>`
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

Импорт:

```powershell
python -m brucebet.cli --db brucebet.sqlite import --reset `
  --participants participants.csv `
  --teams teams.csv `
  --matches matches.csv `
  --predictions predictions.csv `
  --team-form team_form.csv `
  --absences absences.csv `
  --contexts match_contexts.csv `
  --odds match_odds.csv `
  --factors team_match_factors.csv `
  --assessments match_assessments.csv
```

## Runtime Data

Реальные прогнозы, участники, SQLite и выгрузки должны жить только в серверном `data/`.

В публичный GitHub они не коммитятся: там остаются `data/README.md` и `data/.gitkeep`.

## World Cup Legacy

Старый ЧМ-сценарий не удалён из архитектуры: VK-парсер и `configs/world_cup_2026.json` оставлены как совместимый режим. Но активная разработка теперь идёт под EPL-longterm: сезонность, профили участников, риск-карта, стратегия и пост-туровый разбор.
