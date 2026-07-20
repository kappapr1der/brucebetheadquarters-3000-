# Data Sources

BruceBet should use sources in layers. One source should not decide the pick by itself.

## Best default stack

1. Official Premier League pages/public API: schedule, kickoff time, venue, results, suspensions when published.
2. Club strength baselines: ClubElo-style ratings, market value, last-season finish, home/away split.
3. Transfermarkt: squads, market value, player importance, injuries context.
4. Odds market: implied probability and consensus risk. Prefer The Odds API for automation, or manual bookmaker average if API coverage is missing.
5. Sofascore/FotMob/FBref: manual pre-match check for lineups, recent form, xG, match stats, odds movement, and news. Use carefully; do not assume scraping is allowed.
6. Manual analyst note: fixture congestion, Europe rotation, tactical fit, “this smells like 1:1” signals.

## API position

Yes, API access is worth it for automation, but the bot should not depend on one source.

Recommended MVP:

- use `matches.csv` / fixture import for calendar and deadlines;
- use the Premier League public fixtures API (`footballapi.pulselive.com`) for the official 2026/27 calendar when available;
- use Fantasy Premier League data as a cheap player-status layer;
- keep `player_statuses.csv` as the normalized import format;
- use The Odds API with `soccer_epl`, `eu`, and `h2h,totals` as the default automated market snapshot;
- add a paid provider later only if it gives reliable injuries, lineups, and player stats for EPL.

Target variable budget for strong match analytics: 60-120 variables total, with 25-40 high-signal variables used in the final recommendation. More than that usually adds noise unless the data is clean and backtested.

## Variables and recommended source

| Variable | Primary source | Backup/manual source | CSV |
| --- | --- | --- | --- |
| Kickoff, venue, result | Official Premier League | Club sites/Sofascore | `matches.csv`, `match_contexts.csv` |
| Club strength | ClubElo-style ratings, table, market value | Manual baseline | `teams.csv` |
| Home/away strength | League table splits, xG splits | Manual estimate | `teams.csv`, `team_match_factors.csv` |
| Market value, squad depth | Transfermarkt | Manual estimate | `teams.csv` |
| Form and recent results | Premier League, Sofascore, Transfermarkt | Manual last 5 matches | `team_form.csv` |
| xG and match stats | Sofascore/FotMob/FBref when available | Manual “low/medium/high chance quality” | `team_form.csv` |
| Injuries, suspensions, doubtful players | Official team news first | Transfermarkt, Sofascore/FotMob news, reputable beat reporters | `absences.csv` |
| Player availability/form | Fantasy Premier League API, paid football API | Manual team news | `player_statuses.csv` |
| Rest, travel, weather, pitch | Official schedule, weather services, manual | Manual | `match_contexts.csv` |
| Europe/fixture congestion | UEFA schedule, club schedule | Manual | `match_contexts.csv`, `team_match_factors.csv` |
| Odds 1X2, totals, BTTS | The Odds API / bookmaker average | Manual snapshot | `match_odds.csv` |
| Tactical fit, motivation, rotation risk | Manual | Pre-match reports | `team_match_factors.csv`, `match_assessments.csv` |

## Practical rule

Use automation for stable or structured data: ranking, Elo, odds, schedule, results.

Use manual review for high-context data: injuries, motivation, likely rotation, tactical fit. This is where the bot should collect notes, not pretend to be a doctor, scout, and bookmaker at the same time.

## The Odds API usage

Default env:

- `THE_ODDS_API_SPORT=soccer_epl`
- `THE_ODDS_API_REGIONS=eu`
- `THE_ODDS_API_MARKETS=h2h,totals`

This costs 2 credits per successful odds sync. `/sports` and `brucebet quota` are used for health/quota checks and do not spend odds credits.

## Premier League fixtures

The official site public API currently exposes `compSeason=841` for `English Premier League Season 2026/2027`.

Use:

- `brucebet sync-fixtures`
- Telegram `/sync_fixtures`

The importer stores kickoffs in `Europe/Moscow` by default and keeps matchweek order as the contest template order.

## Automated variables

Use:

- `brucebet sync-variables`
- Telegram `/sync_variables`
- `brucebet dossier <match>`
- Telegram `/dossier <match>`

Current automated layer:

- FPL bootstrap API -> `player_status_snapshots`
- ClubElo API -> `teams.elo_rating`
- Official schedule + stadium map -> `match_contexts`
- Open-Meteo -> weather only inside the 16-day forecast horizon
- Derived rest/fatigue/absence factors -> `team_match_factors`
- Draft Elo/odds assessment -> `match_assessments`

The background sync intentionally does not call The Odds API. Odds snapshots should be pulled manually near deadline because each successful odds sync spends credits.

## Links

- Premier League fixtures/results: https://www.premierleague.com/fixtures
- Fantasy Premier League bootstrap API: https://fantasy.premierleague.com/api/bootstrap-static/
- ClubElo: https://clubelo.com/
- Open-Meteo: https://open-meteo.com/
- Transfermarkt: https://www.transfermarkt.com/
- The Odds API: https://the-odds-api.com/
- Sofascore: https://www.sofascore.com/
- FotMob: https://www.fotmob.com/
- FBref: https://fbref.com/
