# tradelite

Small, working trading platform: strategies as plugin files, honest backtests, one shared risk
engine, paper broker, FastAPI backend. Live Zerodha is **deliberately not built yet**.

## Run it with Docker (PostgreSQL included)
```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up --build       # starts PostgreSQL + the app
```
Open **http://localhost:8000/**. The database lives in the `pgdata` volume, so it survives restarts.
The app is bound to `127.0.0.1` only because the API requires a bearer token (`API_TOKEN`); do not expose it publicly without a reverse proxy.

Then, in the dashboard, open **Data** and click *Fetch prices* (or use the command line):
```bash
docker compose exec app python -m tradelite fetch --years 5                 # liquid NSE large caps
docker compose exec app python -m tradelite fetch --symbols RELIANCE,TCS    # your own list
docker compose exec app python -m tradelite import-csv                      # SYMBOL.csv files from ./data
```
Try it without any real data: `TRADELITE_DEMO=1 docker compose up --build` (synthetic prices, proves plumbing only).

## Run it without Docker
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && pytest                # 80 tests
DATABASE_URL=postgresql://user:pass@localhost:5432/tradelite \
  uvicorn tradelite.api.main:create_app --factory --port 8000     # or leave DATABASE_URL unset for SQLite
```
The dashboard is prebuilt in `frontend/dist`. To rebuild or develop it (needs Node 20+):
`cd frontend && npm install && npm run build` (`npm run dev` for hot reload on :5173, API on :8000).

Run the whole test suite on PostgreSQL: `TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tradelite_test pytest`
(it drops and recreates the tables in that database, so use a scratch one).
UI end-to-end test (real seeded server): `python scripts/seed_e2e.py <db-url>`, start the server with
`TRADELITE_DEMO=1 DATABASE_URL=<db-url>` on port 8766, then `cd frontend && TL_API=http://127.0.0.1:8766 npm run test:e2e`.

## Strategies
Seven built in, all long-only daily-bar strategies with an ATR stop and an R-multiple target:
`ema_cross`, `donchian_breakout`, `ema_pullback`, `macd_cross`, `rsi_reversion`, `bollinger_breakout`, `new_high_momentum`
(trend, breakout, pullback, momentum and mean-reversion styles). Each is checked automatically for look-ahead bias.
There is no such thing as "the best strategy": what works depends on the stock and the period, which is why the
platform tests every strategy on every stock and only ranks up the ones that hold up (below).

**Customise**: Backtests page, *Customise this strategy's settings*. Change the numbers, name them, save. Saved settings
are scanned alongside the built-in ones and get their own track record. `GET/POST/DELETE /strategy-configs`.

**Add a strategy**: create `tradelite/strategies/my_strategy.py` with a `Strategy` subclass (`meta`, `prepare`, `on_bar`).
It is auto-discovered and `tests/test_strategies.py` checks it for look-ahead bias.

## What worked, and ranking
A backtest of a strategy (with a given set of settings) on a stock is called **proven** when it has at least 30 trades and is
profitable with a profit factor above 1.2 in both the early 70% and the late 30% of its history. The **Track Record** page
lists every proven pair with the stock, the strategy that worked, the tested dates and the last winning trade.
When a scan creates a signal (and its order), it is matched against that record: an exact match on strategy, settings and
stock is marked **Proven**, everything else **Unproven**. Proven signals are listed first, and in a scan they are routed first,
so they get first claim on limited position slots and capital. Ranking never bypasses the risk engine, the kill switch or the
approval step. It is a stability check on past data, not proof of future profit. Random data correctly produces no proven pairs.

## Live screener
The **Screener** page shows, for each strategy, which stocks match right now. During NSE hours (Mon-Fri 9:15-15:30 IST) a
background loop checks every stored stock every 5 minutes (`LIVE_INTERVAL_SECONDS`, minimum 60) with free Yahoo intraday prices,
builds today's bar so far, and runs the same strategy code the daily scan uses. Each match shows entry, stop loss, target,
reward-to-risk, risk per share and the quantity your capital allows (or why it allows none).

Read it as an early heads-up, not a trade signal:
- A match is **provisional**. The bar is still forming and can fade before the close (shown as "Faded"). The confirmed signal
  comes from the after-close scan and goes to the Desk for your approval. The screener never places orders.
- Strategies were designed and backtested on daily closes. Volume-based rules compare partial-day volume with a full-day average,
  so early in the day they fire less than they would at the close.
- Yahoo intraday data for NSE is unofficial, may be delayed and can fail. Failures are shown on the page, never hidden.
- Alerts are free: the dashboard (refreshes every 30s, plus optional browser notifications) and optional Telegram
  (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). One alert per new match, not per check.
- It screens only stocks you have fetched on the **Data** page.

**1:5 reward-to-risk:** on **Track Record**, choose target 1:5 and *Test all strategies on all stocks*. This saves an `rr5` setting
per strategy and backtests it separately, so you see which strategy and stock pairs actually hold up with a 5x target. The screener
then also shows `rr5` matches, ranked Proven only where that pair passed. A 5x target is much harder to reach, so expect few.

## Real data
Free source: Yahoo Finance via `yfinance` (split/dividend-adjusted daily bars, NSE symbols as `RELIANCE` -> `RELIANCE.NS`).
It is an unofficial API and can rate-limit or change; failures are reported per symbol and never stored as data.
Prices are validated before they are stored (OHLC consistency, no NaN, no duplicates). Set `DATA_SOURCE=csv` to read
`SYMBOL.csv` files (`date,open,high,low,close,volume`) directly from `DATA_DIR` instead of the database.

## Modes (`TRADELITE_MODE`)
`signal_only` records signals · `semi_auto` creates orders that wait for your approval · `auto` sends
risk-approved orders to the broker. Stops/targets are always automatic via `POST /exits/check`.

## Portfolio backtest, benchmark, survivorship

The single-symbol backtest answers "does this rule work on this stock". The portfolio backtest answers
"would this account have made money": all symbols and strategies share ONE account, compete for the position
slots and capital of the same risk engine, and are judged against the market.

```
tradelite fetch --symbols NIFTYBEES              # the benchmark (a Nifty 50 ETF), stored like any symbol
tradelite portfolio --benchmark NIFTYBEES --start 2021-01-01 --risk-free 0.065
tradelite portfolio --liquid-top-n 15 --universe-file universe.csv
```
API: `POST /portfolio-backtests`, `GET /portfolio-backtests[/id]` (stored with trades and equity curves).

* **Fills**: signal at the close, fill at the next open. When signals exceed free slots, the more liquid symbol
  (trailing 20-day traded value) goes first, then strategy order, then name. No future data is used.
* **Comparison**: the system vs buy-and-hold of the benchmark (costs and slippage paid) and vs a daily-rebalanced
  equal-weight of the tradable universe. Reported: CAGR, max drawdown, Sharpe, beta, alpha, average time invested.
  Verdicts: `beats_benchmark`, `better_risk_adjusted_only`, `underperforms`. A system that sits in cash 60% of the time
  can show low CAGR and low drawdown; read return and risk together.
* **Survivorship**: `--universe-file` takes `symbol,start,end` rows (end blank = still a member; a symbol may have several
  rows). See `data/universe.example.csv`. Without it, every run says `bias_risk: HIGH`. `--liquid-top-n` picks
  the most liquid names from prior data each day; it removes hindsight in *which* stocks, not in *which stocks are in the pool*.

**Getting real membership history is on you.** The NSE index owner publishes constituent changes, and NSE's
bhavcopy archive lists every traded security. Neither is wired in here, and free Yahoo data often
does not serve delisted tickers: those show up as `missing_price_data` in the report. Until you have both the
membership file and the prices for removed names, treat every result as an upper bound.

## Running it every day: worker, alerts, auth

```
python -m tradelite token          # -> put it in .env as API_TOKEN (docker compose refuses to start without one)
python -m tradelite test-alert     # after setting TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (and/or ALERT_WEBHOOK_URL)
python -m tradelite run-daily      # one full pass by hand; `worker` does this on a schedule
```
`docker compose up` starts the API and a **worker**. On trading days at `DAILY_RUN_AT` (IST, default 16:30, after the close) it:
refreshes prices -> refuses to trade on stale bars (a symbol without today's bar is skipped and reported; if none has one, the run
FAILS instead of scanning old data) -> checks exits (frees slots) -> scans -> alerts. A failed run is retried after 15 minutes,
three attempts in total, then a critical alert. Everything is idempotent, so retries and restarts cannot double-trade.

* **Holidays**: put exchange holidays in a text file (`YYYY-MM-DD` per line, see `data/holidays.example.txt`) and set `HOLIDAYS_FILE`.
  Nothing is hard-coded; without the file the worker treats a holiday as a trading day, finds no new bar, and raises the stale-data alert.
* **Price refresh downloads the full history each day.** Yahoo prices are dividend/split adjusted; a corporate action rewrites all older bars,
  so a partial refresh would splice two price scales together.
* **Alerts** go to the log, the `alerts` table (`GET /alerts`) and, if configured, Telegram and/or a JSON webhook. You get: signals awaiting your
  approval, stop/target exits, daily loss limit hit, kill switch flipped, stale data, run failed, and (critical) **an exit order the broker
  refused, which leaves a position open**. A broken channel never stops trading code, and bot tokens are never logged.
* **Dead-man's switch**: set `HEALTHCHECK_PING_URL` (e.g. a healthchecks.io check). It is pinged after each *successful* run, so if the worker
  itself dies you hear about it from that service, not from silence. Run history: `GET /jobs`.
* **Auth**: one shared secret in `API_TOKEN`, sent as `Authorization: Bearer ...` (the dashboard asks for it once and keeps it in the browser).
  Everything except `/health` and the static dashboard files needs it, including `/docs`. Tokens under 24 characters are refused at startup.
  With `API_TOKEN` empty auth is OFF (local development) and a warning is logged. This is a single-user gate, not accounts: keep the port on
  127.0.0.1 and put HTTPS in front (reverse proxy) before exposing it, because a bearer token over plain HTTP can be read on the network.
* **Equity is marked to market.** Open positions are valued at the latest close, so an open loss now shrinks new position sizes (before, only
  closed P&L counted). A position with no price is marked at entry and reported as `unpriced_positions`. `GET /account` shows `unrealized_pnl`.

## Guarantees the tests enforce
Fills at next bar open (no look-ahead) · stop beats target inside one bar · gaps fill at the open ·
costs on both legs · backtest and live share the same RiskEngine and exit rule · re-scans never
duplicate orders · stale signals are rejected at approval · kill switch blocks everything.

## Known limits (be honest with yourself)
Daily bars, long-biased strategies, single account, equity-style costs (verify the rates against
Zerodha's calculator), single shared-token auth (no user accounts), no options/forex data yet, the database schema is created on start (no migrations yet: a schema change needs a fresh database or a manual migration).

Portfolio backtest limits: default strategy parameters only (saved presets are not used yet); the daily loss limit
uses the previous trading day's realized P&L (daily bars); positions in symbols whose data ends are closed at the last
close, which flatters a collapse; parameter/strategy selection across many runs is not corrected for luck.
