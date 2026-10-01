# tradelite

Small, working trading platform: strategies as plugin files, honest backtests, one shared risk
engine, paper broker, FastAPI backend. Live Zerodha is **deliberately not built yet**.

## Run it with Docker (PostgreSQL included)
```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up --build       # starts PostgreSQL + the app
```
Open **http://localhost:8000/**. The database lives in the `pgdata` volume, so it survives restarts.
The app is bound to `127.0.0.1` only because the API has no login yet; do not expose it publicly.

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
pip install -e ".[dev]" && pytest                # 64 tests
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

## Real data
Free source: Yahoo Finance via `yfinance` (split/dividend-adjusted daily bars, NSE symbols as `RELIANCE` -> `RELIANCE.NS`).
It is an unofficial API and can rate-limit or change; failures are reported per symbol and never stored as data.
Prices are validated before they are stored (OHLC consistency, no NaN, no duplicates). Set `DATA_SOURCE=csv` to read
`SYMBOL.csv` files (`date,open,high,low,close,volume`) directly from `DATA_DIR` instead of the database.

## Modes (`TRADELITE_MODE`)
`signal_only` records signals · `semi_auto` creates orders that wait for your approval · `auto` sends
risk-approved orders to the broker. Stops/targets are always automatic via `POST /exits/check`.

## Guarantees the tests enforce
Fills at next bar open (no look-ahead) · stop beats target inside one bar · gaps fill at the open ·
costs on both legs · backtest and live share the same RiskEngine and exit rule · re-scans never
duplicate orders · stale signals are rejected at approval · kill switch blocks everything.

## Known limits (be honest with yourself)
Daily bars, long-biased strategies, single account, equity-style costs (verify the rates against
Zerodha's calculator), no auth on the API (localhost only), no options/forex data yet, the database schema is created on start (no migrations yet: a schema change needs a fresh database or a manual migration).
