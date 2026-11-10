# Screener

Live breadth monitor, screener, and daily chart for a custom ticker universe, on the Charles Schwab Trader API.

- **Top Scans**: my universe, gapped-up, +ve since last close, moved up since open, and topd moved up since open. Each bar is count / universe. Click a scan to filter the table to those tickers; click it again, click my universe, or Show all to clear.
- **Watchlist**: upload a txt or csv and Replace or Merge it into `universe.json`. A `ticker,group` header keeps groups; a flat symbol list is enough. The box under the table adds one ticker.
- **Table**: Ticker, Price, % From Open, % Day, Volume, 20D RVOL, Paced RVOL. Click a header once for descending, again for ascending. The sort and the charted ticker survive the refresh.
- **Refresh**: every 5 seconds during 9:30–16:00 ET on trading days; every 5 minutes otherwise (pre-market, after hours, weekends, holidays), so extended-hours prices still show up. The header badge shows the session and the current cadence.
- **Token reminder**: a header badge counts down to the Schwab refresh-token expiry (7 days after `login`). From 2 days out a yellow banner appears; on the last day it turns red. The server logs a warning on each level change and hourly after that, and `meta.token` in `/api/screener-data` has `level`, `login_at`, `expires_at`, `seconds_left`.
- **Chart**: daily candles, volume, and four moving averages (10 EMA red, 20 EMA blue, 50 SMA green, 200 SMA grey). Click an average to show or hide it, edit its period, or switch EMA/SMA. Dark mode toggles the chart background. B&W draws hollow up candles and filled down candles.
- **Order entry**: a collapsible panel under the table (not over the chart). Type or dictate an order, pick a risk preset, review the summary, then Confirm. Nothing is sent before Confirm. Demo mode shows the same panel and dialog and does not place an order.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
SCREENER_MODE=demo python backend.py     # fake data, no Schwab account needed
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The server listens on `0.0.0.0:8000` by default; set `HOST` and `PORT` to change it (for example `HOST=127.0.0.1` to keep it local). Tests: `pip install -r requirements-dev.txt`.

Demo mode always refreshes every 5 seconds. Two demo-only variables help preview the UI: `SCREENER_DEMO_TIME=11:30` pins the time of day (so Paced RVOL shows a mid-session value), and `SCREENER_DEMO_TOKEN_AGE_HOURS=126` simulates a login 126 hours ago (so the expiry banner shows).

## Schwab setup

1. Create an app at [developer.schwab.com](https://developer.schwab.com) with the Market Data product. Set its callback URL, for example `https://127.0.0.1:8182`.
2. Put the credentials in `.env` next to `backend.py` (`cp .env.example .env`, git-ignored), or export them:

   ```bash
   SCHWAB_APP_KEY=...
   SCHWAB_APP_SECRET=...
   SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
   SCHWAB_TOKEN_PATH=            # empty: ./token.json
   ```

   Exported variables win over `.env`.

3. `python backend.py login` once. It opens the Schwab login in your browser and listens on the callback URL. After you sign in and approve, the browser warns about a self-signed certificate on `127.0.0.1:8182`; click Advanced → Proceed and the token is captured. The token is written to `token.json` (git-ignored). On a machine without a browser, use `python backend.py login --manual` and paste the redirected URL back.
4. `python backend.py`. With `SCHWAB_APP_KEY` set the server uses Schwab; without it, demo data. `SCREENER_MODE=schwab|demo` forces either.

The access token lasts about 30 minutes. The running server renews it in the background from the refresh token before it expires, including schwab-py's 5-minute early window, and writes that token file back without dropping the refresh token. The refresh token itself expires 7 days after `login`. The login time comes from `creation_timestamp` in the token file, which is kept across those access-token renewals. Run `login` again when the banner appears. A running server notices the new token within 5 seconds and reloads its Schwab client, so no restart is needed. It also starts without a token (the banner says so) and connects once the first login is done.

## Deploy to Hetzner (Ubuntu 24.04)

The server runs as a systemd service, `market-dashboard`, under a dedicated `market-dashboard` user from `/opt/market-dashboard`, with a venv and settings in `/opt/market-dashboard/.env`.

1. Create the server (any Cloud type works) and SSH in as root or a sudo user. If you attached a Hetzner Cloud Firewall, allow TCP 22 and 8000 there too; it sits in front of ufw.
2. Install:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/drkhuls/Screener/main/deploy.sh -o deploy.sh
   sudo bash deploy.sh
   ```

   `deploy.sh` is safe to re-run; run it again to update. It installs `python3-venv`, `git`, and `ufw`, creates the user, clones or fast-forwards the repo, builds the venv, creates `.env` from `.env.example` (mode 600) if missing, opens OpenSSH and 8000/tcp in ufw, enables ufw, and installs and starts the service. Overrides: `BRANCH`, `REPO_URL`, `APP_DIR`, `APP_USER`, `PORT`, `ALLOW_FROM`.
3. Fill in `.env`:

   ```bash
   sudo -u market-dashboard nano /opt/market-dashboard/.env
   ```

   Set `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL` (exactly as registered at Schwab), and `DASHBOARD_USER` / `DASHBOARD_PASSWORD`. Then `sudo systemctl restart market-dashboard`.
4. Log in to Schwab. Pick one of these:
   - **On the server** (paste flow, no browser needed there):

     ```bash
     sudo -u market-dashboard /opt/market-dashboard/.venv/bin/python /opt/market-dashboard/backend.py login --manual
     ```

     Open the printed URL in your own browser and sign in. The browser then fails to load `https://127.0.0.1:8182/?code=...`; that is expected. Copy that full URL from the address bar and paste it into the terminal within about 30 seconds.
   - **On your computer**: run `python backend.py login` locally with the same app key, then copy the token over and give it to the service user:

     ```bash
     scp token.json root@SERVER:/opt/market-dashboard/token.json
     ssh root@SERVER 'chown market-dashboard: /opt/market-dashboard/token.json && chmod 600 /opt/market-dashboard/token.json'
     ```

   The service picks the token up without a restart. Repeat every 7 days; the dashboard warns 2 days ahead, and `journalctl -u market-dashboard` logs the warning too. Always run the server-side login as `market-dashboard` (with `sudo -u`), so the token stays owned by and writable for the service. schwab-py rewrites it whenever it refreshes the access token.
5. Check: `systemctl status market-dashboard`, `journalctl -u market-dashboard -f`, and `http://SERVER_IP:8000`.

### Security

The dashboard has no accounts of its own, and `0.0.0.0:8000` is reachable from the internet.

- Set `DASHBOARD_USER` and `DASHBOARD_PASSWORD` in `.env`. Every page and API call then requires HTTP basic auth, and the server logs a warning at startup if it listens publicly without them. Basic auth over plain HTTP sends the password unencrypted, so also do one of the following.
- Open port 8000 only to your own IP: `sudo ALLOW_FROM=203.0.113.7 bash deploy.sh`, or by hand:

  ```bash
  sudo ufw delete allow 8000/tcp
  sudo ufw allow from 203.0.113.7 to any port 8000 proto tcp
  ```

- Or keep it private with an SSH tunnel: set `HOST=127.0.0.1`, run `sudo ufw delete allow 8000/tcp`, and use `ssh -L 8000:127.0.0.1:8000 root@SERVER`. Then open `http://127.0.0.1:8000` locally.

`.env` and `token.json` are mode 600 and owned by `market-dashboard`. They are git-ignored; never commit them.

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
- `rvol_pace = volume / (avg_volume_20d * share of the 9:30–16:00 session elapsed)` (Paced RVOL column). Blank before the open; equal to `rvol` after the close.
- Outside the regular session Schwab's `quote.lastPrice` stays at the 16:00 print, so Price uses `extended.lastPrice` when its trade is newer (shown in purple; hover for the regular close). `prev_close` stays the prior session's `closePrice`, so % Day after hours is the extended price against yesterday's close. The chart's daily bar keeps the regular-session close.
- Fields whose inputs are missing or zero (for example open before 9:30) show `—` and are not counted.

## Order entry

The panel sits under the screener table. Presets are 0.25%, 0.33%, 0.5%, and 1%. **0.5% is selected by default.** A percent in the instruction (for example `with 1% stop`) overrides the button. The mic button uses the browser Speech Recognition API (`webkitSpeechRecognition`).

Review parses the text and opens a confirmation dialog. That step reads the account equity and the quote. It does not call Schwab `place_order`. Confirm is the only action that can send. Cancel sends nothing. In demo mode Confirm describes the order and does not call Schwab; demo equity is a fixed $250,000 so the dialog has a number to show.

**Default order.** A ticker on its own, or `buy NVDA` with no share count, is a **buy**, **market**, **stop-market at the low of day**. The share count is

```
floor(account_equity × risk% / (last − low_of_day))
```

using the **last price read when Confirm is accepted**. With the default 0.5% risk the budget is `equity × 0.005`. On the demo account that is $1,250. If NVDA's last is $140 and the low of day is $135, the distance is $5 and the order is **250 shares**. If the last is at or below the low, the distance is zero and the order is rejected before any division. An explicit share count (`buy 100 TSLA`) is used as given; the dialog still shows the risk budget and the stop.

If the live quote at Confirm changes the share count, the stop, or the equity in the dialog, the dialog updates and **nothing is sent** until you confirm the new sentence.

Sells are **sell-short** with a **buy-to-cover stop-market**. With no share count the stop is the high of day and the distance is `high − last`. `sell 50 AAPL limit 185 with 1% stop` is a limit sell-short of 50 shares; the 1% is the equity-risk budget, not a price offset. Buys and sells may be market or limit. Every stop is stop-market. Stocks and ETFs only: options and futures are rejected.

**Schwab order path.** One `place_order` of a TRIGGER order built with `schwab.orders.common.first_triggers_second`. The entry is the parent. The child is `OrderType.STOP`, which schwab-py defines as stop-market (Schwab submits a market order when the stop is touched). Session is `NORMAL`, duration `DAY`. OCO is not used: an OCO would rest the stop at the same time as the entry, and the stop must wait until the entry fills. If the trigger is rejected, the entry is not sent by itself. Equity is `currentBalances.equity` on the first linked account, or `liquidationValue` when equity is missing. Placing orders needs Accounts and Trading on the Schwab app, not only Market Data. The server polls the trigger and raises an alert when the stop child fills or the order is rejected.

**Alerts.** Submit, stop fill, and errors (parse, reject, API) show an in-app popup and a browser notification. The same text is emailed when `ORDER_ALERT_EMAIL` and `SMTP_HOST` are set. With the address set and SMTP unset, the server logs the email instead of sending it. `ORDER_ALERT_PHONE` is an SMS placeholder: the server logs the message and does not contact a carrier. Both variables are empty in `.env.example`. Set them only in the untracked `.env`.

## Tests

```bash
python -m pytest tests
```
