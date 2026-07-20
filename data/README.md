# Runtime data

This directory is mounted as a Docker volume in production.

Keep real contest data here on the server:

- `participants.csv`
- `vk_matches.csv`
- `vk_predictions.csv`
- `forecasters.sqlite`
- parser/audit outputs
- `snapshots/` - optional private git repo with sanitized CSV/JSON exports

The public GitHub repository intentionally excludes real participant forecasts and SQLite files.
