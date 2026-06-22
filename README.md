# BruceBet 3000

MVP для конкурса прогнозов: считает очки, тай-брейки, поле прогнозов и сценарии по конкретному матчу.

Плюс ведет базу переменных для прогноза: команды, форма, травмы/дисквалификации, коэффициенты, контекст матча, мотивация, усталость, ротация и ручная оценка риска.

## Что уже зашито

- Точный счет: 3 очка.
- Та же разница: 2 очка.
- Тот же исход: 1 очко.
- Иначе: 0 очков.
- Стандартный счет: `0:0`-`9:9`.
- `2-0`, `2;0`, `2 : 0` принимаются и нормализуются в `2:0`.
- `10:0`, лишние слова и другой мусор считаются невалидными.
- Дедлайн по матчу: прогноз должен быть отправлен не позднее чем за 90 минут до начала матча.
- Если прогноз тура отправлен до общего дедлайна тура, все матчи считаются допустимыми.
- Если прогноз отправлен после общего дедлайна тура, допустимость считается поматчево: матч засчитывается только если до его kickoff ещё больше 90 минут.
- Тай-брейки: очки, точные счета, разницы, затем очки в последнем туре, предпоследнем и так далее.
- Призовой фонд: `paid=true` участники по 300 рублей, топ-3 получают 50/30/20%.

## Быстрый старт

```powershell
python -m brucebet.cli --db brucebet.sqlite load-sample
python -m brucebet.cli --db brucebet.sqlite table
python -m brucebet.cli --db brucebet.sqlite field Belgium
python -m brucebet.cli --db brucebet.sqlite scenario Belgium 1:1
python -m brucebet.cli --db brucebet.sqlite vs Bruce Igor
python -m brucebet.cli --db brucebet.sqlite team Belgium
python -m brucebet.cli --db brucebet.sqlite dossier Belgium
python -m brucebet.cli --db brucebet.sqlite recommend Belgium
python -m brucebet.cli --db brucebet.sqlite deadlines
python -m brucebet.cli --db brucebet.sqlite audit
```

Если `python` не находится в Windows-среде Codex, используй bundled runtime:

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m brucebet.cli --db brucebet.sqlite load-sample
```

## CSV-форматы

`examples/participants.csv`

```csv
name,paid
Bruce,true
Igor,true
Guest,false
```

`examples/matches.csv`

```csv
round,position,home,away,kickoff_at,result
1,1,Belgium,Iran,2026-06-22T18:00:00+03:00,0:0
1,2,Uruguay,Cape Verde,2026-06-22T21:00:00+03:00,
```

`examples/predictions.csv`

```csv
participant,round,position,score,submitted_at,source
Bruce,1,1,0:0,2026-06-22T15:00:00+03:00,telegram
Igor,1,1,1:0,2026-06-22T15:00:00+03:00,telegram
```

Импорт своих данных:

```powershell
python -m brucebet.cli --db brucebet.sqlite import --reset --participants participants.csv --teams teams.csv --matches matches.csv --predictions predictions.csv
```

Полный импорт с переменными:

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

## Команды

- `table` - общая таблица, точные, разницы, исходы, опоздания и призовые.
- `match Belgium` - прогнозы и очки по матчу.
- `field Belgium` - консенсус поля: `P1/X/P2` и популярные счета.
- `recommend Belgium` - структурная рекомендация: базовый счёт, риск, уверенность, поле и контр-сценарий.
- `deadlines` - дедлайны туров; если есть kickoff первого матча, дедлайн считается как `kickoff - 90 минут`.
- `scenario Belgium 1:1` - кто сколько получит при сценарии `1:1`.
- `vs Bruce Igor` - где Bruce отличается от Igor и сколько уже выиграл/проиграл на этих отличиях.
- `team Belgium` - паспорт команды: сила, стиль, форма, травмы/дисквалификации.
- `dossier Belgium` - матчевое досье: команды, контекст, коэффициенты, факторы, отсутствующие, ручная оценка.
- `audit` - пропущенные прогнозы, нечитаемые счета, нестандартные принятые форматы и поздние отправки.
- `parse-vk pasted-text.txt --out-dir data` - разобрать VK-пасту Forecasters Club в CSV.
- `brucebet/service_messages.py` - сервисные ответы бота: принято, следующий шаг, аудит, дедлайн.
- В сервисных сообщениях уже есть расписание напоминаний: за 24 часа, 6 часов, 3 часа, 1 час и 20 минут до дедлайна, плюс сообщение после дедлайна.

## Реальные данные Forecasters Club

В `data/` лежит первая загрузка из присланного треда:

- `participants.csv` - 16 участников и отметка взноса.
- `vk_matches.csv` - 2 тура по 24 матча, с дедлайнами тура.
- `vk_predictions.csv` - распарсенные прогнозы из VK-пасты.
- `vk_parse_summary.txt` - краткий отчёт парсинга.
- `forecasters.sqlite` - SQLite-база после импорта.

Текущий пользователь в этой базе: `Bruce Wayne`.

Команды для пересборки:

```powershell
python -m brucebet.cli parse-vk pasted-text.txt --out-dir data
python -m brucebet.cli --db data\forecasters.sqlite import --reset --participants data\participants.csv --matches data\vk_matches.csv --predictions data\vk_predictions.csv
python -m brucebet.cli --db data\forecasters.sqlite audit
```

Для корректного частичного допуска поздних прогнозов нужно заполнить `kickoff_at` в `vk_matches.csv`. Без kickoff-времени поздний после общего дедлайна прогноз считается недоказанно поздним.

## Переменные для прогноза

Шаблоны лежат в `examples/`:

- `teams.csv` - рейтинг FIFA, Elo, стоимость состава, стиль, сила атаки/защиты.
- `team_form.csv` - последние матчи, голы, xG, важность матча.
- `absences.csv` - травмы, дисквалификации, сомнительные игроки, сила влияния.
- `match_contexts.csv` - стадион, погода, отдых, переезд, мотивация, риск ротации.
- `match_odds.csv` - 1X2, тоталы, BTTS, время снимка коэффициентов.
- `team_match_factors.csv` - факторы конкретной команды в конкретном матче.
- `match_assessments.csv` - наша ручная оценка: риск, уверенность, контр-сценарий, предложенный счет.

Рекомендованная карта источников описана в `DATA_SOURCES.md`.

## Следующий слой

После загрузки реальных участников и матчей можно подключать Telegram:

- `/table`
- `/field <матч>`
- `/scenario <матч> <счет>`
- `/vs <игрок>`
- `/team <команда>`
- `/dossier <матч>`
- напоминания за 3 часа, 1 час и 20 минут до дедлайна

Ядро уже отделено от интерфейса, поэтому Telegram-бот будет тонким адаптером поверх SQLite.

Целевая логика бота описана в `BOT_SPEC.md`. Профиль текущего турнира лежит в `configs/world_cup_2026.json`, шаблон будущего АПЛ-режима - в `configs/epl_template.json`.

Сервисные сообщения для Telegram-бота вынесены в `brucebet/service_messages.py`: после твоего прогноза бот должен отвечать "принято", показывать сколько матчей увидел, подсвечивать нестандартные принятые форматы/пропущенные строки, ставить напоминания перед дедлайном и просить прогнозы участников следующим шагом.

## Telegram deploy

Telegram-оболочка уже есть: `brucebet/telegram_app.py`.

Локальный запуск:

```powershell
$env:TELEGRAM_BOT_TOKEN="..."
python -m brucebet.telegram_app
```

Docker-запуск на сервере:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f brucebet
```

Подробно: `DEPLOY.md`.

Команды Telegram:

- `/start`
- `/load`
- `/table`
- `/field <матч>`
- `/recommend <матч>`
- `/match <матч>`
- `/vs <участник>`
- `/deadlines`
- `/schedule`
- `/audit`
