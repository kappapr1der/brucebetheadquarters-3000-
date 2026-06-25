# Data Sources

BruceBet should use sources in layers. One source should not decide the pick by itself.

## Best default stack

1. Official Premier League pages: schedule, kickoff time, venue, results, suspensions when published.
2. Club strength baselines: ClubElo-style ratings, market value, last-season finish, home/away split.
3. Transfermarkt: squads, market value, player importance, injuries context.
4. Odds market: implied probability and consensus risk. Prefer The Odds API for automation, or manual bookmaker average if API coverage is missing.
5. Sofascore/FotMob/FBref: manual pre-match check for lineups, recent form, xG, match stats, odds movement, and news. Use carefully; do not assume scraping is allowed.
6. Manual analyst note: fixture congestion, Europe rotation, tactical fit, “this smells like 1:1” signals.

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
| Rest, travel, weather, pitch | Official schedule, weather services, manual | Manual | `match_contexts.csv` |
| Europe/fixture congestion | UEFA schedule, club schedule | Manual | `match_contexts.csv`, `team_match_factors.csv` |
| Odds 1X2, totals, BTTS | The Odds API / bookmaker average | Manual snapshot | `match_odds.csv` |
| Tactical fit, motivation, rotation risk | Manual | Pre-match reports | `team_match_factors.csv`, `match_assessments.csv` |

## Practical rule

Use automation for stable or structured data: ranking, Elo, odds, schedule, results.

Use manual review for high-context data: injuries, motivation, likely rotation, tactical fit. This is where the bot should collect notes, not pretend to be a doctor, scout, and bookmaker at the same time.

## Links

- Premier League fixtures/results: https://www.premierleague.com/fixtures
- ClubElo: https://clubelo.com/
- Transfermarkt: https://www.transfermarkt.com/
- The Odds API: https://the-odds-api.com/
- Sofascore: https://www.sofascore.com/
- FotMob: https://www.fotmob.com/
- FBref: https://fbref.com/
