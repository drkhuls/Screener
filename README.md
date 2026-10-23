# Screener

Live breadth monitor, screener, and daily chart for a custom ticker universe, on the Charles Schwab Trader API.

- **Top Scans**: my universe, gapped-up, +ve since last close, moved up since open, and topd moved up since open. Each bar is count / universe.
- **Table**: Ticker, Price, % From Open, % Day, Volume, 20D RVOL. Click a header once for descending, again for ascending. The sort and the charted ticker survive the 5-second refresh.
- **Chart**: daily candles, volume, 10 EMA red, 20 EMA blue, 50 SMA green, 200 SMA grey. Light background by default; the button switches it to dark.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SCREENER_MODE=demo python backend.py     # fake data, no Schwab account needed
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Schwab setup

1. Create an app at [developer.schwab.com](https://developer.schwab.com) with the Market Data product. Set its callback URL, for example `https://127.0.0.1:8182`.
2. Export the credentials:

   ```bash
   export SCHWAB_APP_KEY=...
   export SCHWAB_APP_SECRET=...
   export SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
   # optional: export SCHWAB_TOKEN_PATH=/path/to/token.json  (default ./token.json)
   ```

3. `python backend.py login` once. It prints a Schwab login URL; after you sign in, paste the redirected URL back. The token is written to `token.json` (git-ignored).
4. `python backend.py`. With `SCHWAB_APP_KEY` set the server uses Schwab; without it, demo data. `SCREENER_MODE=schwab|demo` forces either.

The refresh token expires after 7 days. Run `login` again when the server reports a token error.

## Tickers

Edit `universe.json`. Groups are labels shown next to each ticker; the screener uses one flat list with duplicates removed. You can also put symbols in `"tickers"`, or replace the whole file with a plain JSON list. The server picks up changes within 5 seconds and loads history for new tickers. Quotes are requested in chunks of `quote_chunk_size` (100), so lists over 100 work.

## Settings (`config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_seconds` | 5 | Quote refresh during market hours |
| `off_hours_poll_seconds` | 300 | Quote refresh outside 9:30–16:00 ET |
| `history_init_time_et` | 09:15 | Daily reload of prev close, 20 SMA, 20-day average volume |
| `stats_history_days` | 45 | Calendar days of history for the daily stats |
| `chart_history_days` | 730 | Calendar days of history for charts (the 200 SMA needs ~10 months) |
| `quote_chunk_size` | 100 | Symbols per `get_quotes` call |
| `history_requests_per_minute` | 100 | Throttle for price-history calls (Schwab allows 120/min) |
| `topd_min_pct_from_open` | 2.0 | topd: % from open at or above this |
| `topd_min_rvol` | 1.2 | topd: RVOL at or above this |
| `exclude_groups_from_breadth` | `[]` | Groups left out of the Top Scans counts, e.g. `["Indexes"]` |

## Formulas

- `pct_from_open = (price - open) / open * 100`
- `pct_from_close = (price - prev_close) / prev_close * 100`, where `prev_close` is the quote's `closePrice`, or the last completed daily bar if that is missing
- `rvol = volume / avg_volume_20d`, with the average over the last 20 completed sessions. Hover a RVOL cell for the time-of-day paced value, `volume / (avg_volume_20d * share of session elapsed)`.
- Fields whose inputs are missing or zero (for example open before 9:30) show `—` and are not counted.

## Tests

```bash
python -m pytest tests
```
