# Screener

Live breadth monitor, screener, and daily chart for a custom ticker universe, on the Charles Schwab Trader API.

- **Top Scans**: my universe, gapped-up, +ve since last close, moved up since open, and topd moved up since open. Each bar is count / universe. Click a scan to filter the table to those tickers; click it again, click my universe, or Show all to clear.
- **Watchlist**: upload a txt or csv and Replace or Merge it into `universe.json`. A `ticker,group` header keeps groups; a flat symbol list is enough. The box under the table adds one ticker.
- **Table**: Ticker, Price, % From Open, % Day, Volume, 20D RVOL. Twenty tickers per page, with page numbers when a scan or the universe has more. Click a header once for descending, again for ascending. The sort, the scan filter, and the charted ticker survive a page change.
- **Order entry**: a collapsible panel under the table. Type or dictate an order, pick a risk preset, review the summary, then Confirm. Nothing is sent before Confirm. Demo mode shows the same panel and dialog and does not place an order.
- **Refresh**: every 5 seconds during 9:30–16:00 ET on trading days; every 5 minutes otherwise (pre-market, after hours, weekends, holidays), so extended-hours prices still show up. The header badge shows the session and the current cadence.
- **Token reminder**: a header badge counts down to the Schwab refresh-token expiry (7 days after `login`). From 2 days out a yellow banner appears; on the last day it turns red. The server logs a warning on each level change and hourly after that, and `meta.token` in `/api/screener-data` has `level`, `login_at`, `expires_at`, `seconds_left`.
- **Chart**: daily candles, volume, and four moving averages (10 EMA red, 20 EMA blue, 50 SMA green, 200 SMA grey). Click an average to show or hide it, edit its period, or switch EMA/SMA. Dark mode toggles the chart background. B&W draws hollow up candles and filled down candles.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SCREENER_MODE=demo python backend.py     # fake data, no Schwab account needed
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Demo mode always refreshes every 5 seconds. Two demo-only variables help preview the UI: `SCREENER_DEMO_TIME=11:30` pins the time of day, and `SCREENER_DEMO_TOKEN_AGE_HOURS=126` simulates a login 126 hours ago (so the expiry banner shows).

## Schwab setup

1. Create an app at [developer.schwab.com](https://developer.schwab.com) with the Market Data product. Set its callback URL, for example `https://127.0.0.1:8182`.
2. Export the credentials:

   ```bash
   export SCHWAB_APP_KEY=...
   export SCHWAB_APP_SECRET=...
   export SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
   # optional: export SCHWAB_TOKEN_PATH=/path/to/token.json  (default ./token.json)
   ```

3. `python backend.py login` once. It opens the Schwab login in your browser and listens on the callback URL. After you sign in and approve, the browser warns about a self-signed certificate on `127.0.0.1:8182`; click Advanced → Proceed and the token is captured. The token is written to `token.json` (git-ignored). On a machine without a browser, use `python backend.py login --manual` and paste the redirected URL back.
4. `python backend.py`. With `SCHWAB_APP_KEY` set the server uses Schwab; without it, demo data. `SCREENER_MODE=schwab|demo` forces either.

The refresh token expires 7 days after `login`. The login time comes from `creation_timestamp` in the token file, which schwab-py keeps when it refreshes the access token. Run `login` again when the banner appears.

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
| `exclude_groups_from_breadth` | `[]` | Groups left out of the Top Scans counts, e.g. `["Indexes"]`. Index ETFs are counted by default |
| `token_lifetime_days` | 7 | Schwab refresh-token lifetime |
| `token_warn_days` | 2 | Show the yellow banner this many days before expiry |
| `token_urgent_days` | 1 | Turn the banner red this many days before expiry |

## Formulas

- `pct_from_open = (price - open) / open * 100`
- `pct_from_close = (price - prev_close) / prev_close * 100`, where `prev_close` is the quote's `closePrice`, or the last completed daily bar if that is missing
- `rvol = volume / avg_volume_20d`, with the average over the last 20 completed sessions (20D RVOL column).
- Outside the regular session Schwab's `quote.lastPrice` stays at the 16:00 print, so Price uses `extended.lastPrice` when its trade is newer (shown in purple; hover for the regular close). `prev_close` stays the prior session's `closePrice`, so % Day after hours is the extended price against yesterday's close. The chart's daily bar keeps the regular-session close.
- Fields whose inputs are missing or zero (for example open before 9:30) show `—` and are not counted.

## Order entry

The panel sits under the screener table. Presets are 0.25%, 0.33%, 0.5%, and 1%. **0.5% is selected by default.** A percent in the instruction (for example `with 1% stop`) overrides the button. The mic button uses the browser Speech Recognition API.

Review parses the text and opens a confirmation dialog. That step reads the account equity and the quote. It does not call Schwab `place_order`. Confirm is the only action that can send. Cancel sends nothing. In demo mode Confirm describes the order and does not call Schwab; demo equity is a fixed $250,000.

**Default order.** A ticker on its own, or `buy NVDA` with no share count, is a **buy**, **market**, **stop-market at the low of day**. The share count is

```
floor(account_equity × risk% / (last − low_of_day))
```

using the last price read when Confirm is accepted. With the default 0.5% risk the budget is `equity × 0.005`. On the demo account that is $1,250. If NVDA's last is $140 and the low of day is $135, the distance is $5 and the order is **250 shares**. If the last is at or below the low, the distance is zero and the order is rejected before any division.

If the live quote at Confirm changes the share count, the stop, or the equity in the dialog, the dialog updates and nothing is sent until that new sentence is confirmed.

Sells are sell-short with a buy-to-cover stop-market. Buys and sells may be market or limit. Every stop is stop-market. Stocks and ETFs only: options and futures are rejected.

**Schwab order path.** Confirm sends one `place_order` of a TRIGGER order built with `schwab.orders.common.first_triggers_second`. The entry is the parent and the child is a stop-market (`OrderType.STOP`). Session is `NORMAL`, duration `DAY`. Equity is `currentBalances.equity` on the first linked account, or `liquidationValue` when equity is missing. Placing orders needs Accounts and Trading on the Schwab app, not only Market Data.

**Alerts.** Submit, stop fill, and errors show an in-app popup and a browser notification. The same text is emailed when `ORDER_ALERT_EMAIL` and `SMTP_HOST` are set. With the address set and SMTP unset, the server logs the email instead of sending it. `ORDER_ALERT_PHONE` is an SMS placeholder: the server logs the message and does not contact a carrier. Leave both unset to skip them.

## Tests

```bash
python -m pytest tests
```
