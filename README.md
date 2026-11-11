# Screener

Live breadth monitor, screener, and daily chart for a custom ticker universe, on the Charles Schwab Trader API.

- **Top Scans**: my universe, gapped-up, +ve since last close, moved up since open, and topd moved up since open. Each bar is count / universe. Click a scan to filter the table to those tickers; click it again, click my universe, or Show all to clear.
- **Watchlist**: upload a txt or csv and Replace or Merge it into `universe.json`. A `ticker,group` header keeps groups; a flat symbol list is enough. The box under the table adds one ticker.
- **Table**: Ticker, Price, % From Open, % Day, Volume, 20D RVOL. Twenty tickers per page, with page numbers when a scan or the universe has more. Click a header once for descending, again for ascending. The sort, the scan filter, and the charted ticker survive a page change.
- **Order entry**: a row under the table. Buy or Sell, order type (Market, Limit, Stop), time in force, shares (default 100, step 10), and a ticker. Review opens the details page. Send is the only step that can reach Schwab.
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

Schwab allows one product per app, so this server uses two apps and two token files. Quotes never call the trading app. Send never calls the market-data app.

1. At [developer.schwab.com](https://developer.schwab.com), create the market-data app with **Market Data Production**. Create a second app with **Accounts and Trading Production**. Each callback must match that app. The trading callback `https://127.0.0.1:8182` has to be **Ready For Use** before `login-trade` works. `Approved - Pending` is not usable.
2. On the server, the app lives in `/opt/Screener` and the systemd service is named `Screener`. Copy `.env.example` to `/opt/Screener/.env` (mode 600) and fill in the names below. Leave the values out of git.

   Market-data app (quotes, history, candles, the 7-day banner):

   ```bash
   SCHWAB_APP_KEY=
   SCHWAB_APP_SECRET=
   SCHWAB_CALLBACK_URL=
   SCHWAB_TOKEN_PATH=
   ```

   Trading app (account lookup and `place_order` only):

   ```bash
   SCHWAB_TRADE_APP_KEY=
   SCHWAB_TRADE_APP_SECRET=
   SCHWAB_TRADE_CALLBACK_URL=
   SCHWAB_TRADE_TOKEN_PATH=
   ```

   An empty callback uses `https://127.0.0.1:8182`. An empty quote token path uses `token.json` next to `backend.py`. An empty trade token path uses `token-trade.json` next to the quote token. Both token files are mode 600.
3. Quote login, from `/opt/Screener`: `python backend.py login --manual`. It writes only the quote token. Paste the redirected URL back. A browser on that machine can use `python backend.py login` without `--manual`.
4. After the trading app is **Ready For Use**, from `/opt/Screener`: `python backend.py login-trade --manual`. It writes only `token-trade.json`. The quote token is not opened. Run it as the user the `Screener` service runs as, so the service can refresh that file.
5. `python backend.py`. With `SCHWAB_APP_KEY` set the server uses Schwab for quotes; without it, demo data. `SCREENER_MODE=schwab|demo` forces either. Demo Send does not call Schwab.

Each client refreshes its own access token from its own refresh token. A missing trading token does not stop quote polling. Review still quotes. Send says to run `login-trade` instead of dumping a 401. The quote loop notices a new quote token or a new trade token within a few seconds, so a login does not need a restart. Changing `.env` does. From `/opt/Screener`:

```bash
sudo systemctl restart Screener
```

The market-data refresh token expires 7 days after `login`. The banner reads that quote token only. Run `python backend.py login` again when it appears. The trading token expires on its own clock; run `login-trade` again for that one.

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

The row under the table is Buy (green, toggles to red Sell), order type, time in force, a share box, and a ticker. Shares start at 100 and the up/down controls change them by 10. The ticker box takes one symbol, not a sentence. Limit and Stop show a price field. Market and Stop are Day only. Limit can be Day, GTC, EXT, GTC EXT, EXTO, GTC EXTO, AM, or PM. EXTO and GTC EXTO are hidden unless the order type is Limit.

Review opens a details page and reads a quote. It does not look up the account and it does not call `place_order`. Send and Cancel are on that page. Cancel sends nothing. Send is the only step that can reach Schwab. Demo mode describes the order and does not call Schwab.

**Schwab fields.** Time in force is `duration` plus `session`:

| Ticket | duration | session |
| --- | --- | --- |
| Day | `DAY` | `NORMAL` |
| GTC | `GOOD_TILL_CANCEL` | `NORMAL` |
| EXT | `DAY` | `SEAMLESS` |
| GTC EXT | `GOOD_TILL_CANCEL` | `SEAMLESS` |
| EXTO | `DAY` | `EXTO` |
| GTC EXTO | `GOOD_TILL_CANCEL` | `EXTO` |
| AM | `DAY` | `AM` |
| PM | `DAY` | `PM` |

The order is a single equity leg (`BUY` or `SELL`), `MARKET`, `LIMIT`, or `STOP`. Review reads a quote from the market-data app. Send looks up the account and places the order on the trading app only. If that app's key or token is missing, Send says to run `python backend.py login-trade --manual` from `/opt/Screener`. A trading token that Schwab rejects still shows Schwab's 401 text. Demo mode does not call either client.

**Alerts.** Send, fill, and errors show an in-app popup. The same text is emailed when `ORDER_ALERT_EMAIL` and `SMTP_HOST` are set. With the address set and SMTP unset, the server logs the email instead of sending it. `ORDER_ALERT_PHONE` is an SMS placeholder: the server logs the message and does not contact a carrier. Leave both unset to skip them.

## Tests

```bash
python -m pytest tests
```
