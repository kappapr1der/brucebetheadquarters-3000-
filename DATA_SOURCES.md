# Data Sources

BruceBet should use sources in layers. One source should not decide the pick by itself.

## Best default stack

1. Official tournament/FIFA pages: schedule, kickoff time, venue, official ranking, squads, suspensions when published.
2. World Football Elo Ratings: team strength baseline and strength gap.
3. Transfermarkt: squads, market value, manager, player importance, team news context.
4. Odds market: implied probability and consensus risk. Prefer The Odds API for automation, or manual bookmaker average if API coverage is missing.
5. Sofascore/FotMob: manual pre-match check for lineups, recent form, match stats, odds movement, and news. Use carefully; do not assume scraping is allowed.
6. Manual analyst note: motivation, rotation risk, tactical fit, and “this smells like 0:0” signals.

## Variables and recommended source

| Variable | Primary source | Backup/manual source | CSV |
| --- | --- | --- | --- |
| Kickoff, venue, result | Official tournament/FIFA | Transfermarkt/Sofascore | `matches.csv`, `match_contexts.csv` |
| FIFA rank | FIFA ranking | FotMob/Sofascore ranking mirrors | `teams.csv` |
| Elo rating | World Football Elo Ratings | ClubElo-style mirrors if national data unavailable | `teams.csv` |
| Market value, squad depth | Transfermarkt | Manual estimate | `teams.csv` |
| Form and recent results | Official/FIFA, Sofascore, Transfermarkt | Manual last 5 matches | `team_form.csv` |
| xG and match stats | Sofascore/FotMob/FBref when available | Manual “low/medium/high chance quality” | `team_form.csv` |
| Injuries, suspensions, doubtful players | Official team news first | Transfermarkt, Sofascore/FotMob news, reputable beat reporters | `absences.csv` |
| Rest, travel, weather, pitch | Official schedule, weather services, manual | Manual | `match_contexts.csv` |
| Odds 1X2, totals, BTTS | The Odds API / bookmaker average | Manual snapshot | `match_odds.csv` |
| Tactical fit, motivation, rotation risk | Manual | Pre-match reports | `team_match_factors.csv`, `match_assessments.csv` |

## Practical rule

Use automation for stable or structured data: ranking, Elo, odds, schedule, results.

Use manual review for high-context data: injuries, motivation, likely rotation, tactical fit. This is where the bot should collect notes, not pretend to be a doctor, scout, and bookmaker at the same time.

## Links

- FIFA men's ranking: https://inside.fifa.com/fifa-world-ranking/men
- World Football Elo Ratings: https://www.eloratings.net/
- Transfermarkt: https://www.transfermarkt.com/
- The Odds API: https://the-odds-api.com/
- Sofascore: https://www.sofascore.com/
- FotMob: https://www.fotmob.com/
