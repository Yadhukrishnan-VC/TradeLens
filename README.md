# tradelens

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
docker compose exec app python -m tradelens fetch --years 5                 # liquid NSE large caps
docker compose exec app python -m tradelens fetch --symbols RELIANCE,TCS    # your own list
docker compose exec app python -m tradelens import-csv                      # SYMBOL.csv files from ./data
```
Try it without any real data: `TRADELENS_DEMO=1 docker compose up --build` (synthetic prices, proves plumbing only).

## Run it without Docker
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && pytest                # ~300 tests
DATABASE_URL=postgresql://user:pass@localhost:5432/tradelens \
  uvicorn tradelens.api.main:create_app --factory --port 8000     # or leave DATABASE_URL unset for SQLite
```
The dashboard is prebuilt in `frontend/dist`. To rebuild or develop it (needs Node 20+):
`cd frontend && npm install && npm run build` (`npm run dev` for hot reload on :5173, API on :8000).

Run the whole test suite on PostgreSQL: `TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tradelens_test pytest`
(it drops and recreates the tables in that database, so use a scratch one).
UI end-to-end test (real seeded server): `python scripts/seed_e2e.py <db-url>`, start the server with
`TRADELENS_DEMO=1 DATABASE_URL=<db-url>` on port 8766, then `cd frontend && TL_API=http://127.0.0.1:8766 npm run test:e2e`.

## Strategies
Ten built in, all long-only daily-bar strategies with a stop and an R-multiple target.
Classic: `ema_cross`, `donchian_breakout`, `ema_pullback`, `macd_cross`, `rsi_reversion`, `bollinger_breakout`, `new_high_momentum`.
Smart Money Concepts: `smc_fvg`, `smc_order_block`, `smc_sweep` (rules below). Each is checked automatically for look-ahead bias.
There is no such thing as "the best strategy": what works depends on the stock and the period, which is why the
platform tests every strategy on every stock and only ranks up the ones that hold up (below).

## Smart Money Concepts (SMC) strategies

`tradelens/smc.py` turns the usual SMC vocabulary into explicit rules, computed in one forward pass (nothing at bar i uses a later bar).
SMC terms are defined differently by different traders; **these are my definitions, written down so you can check them against
yours** (and so the tests can pin them). Change a rule in `smc.py` and its test together.

| Concept | Rule here |
|---|---|
| Swing high / low | bar whose high (low) is strictly beyond the `swing_left` bars before it and at least as far as the `swing_right` bars after it. **Only known `swing_right` bars later**, never at the pivot bar. |
| BOS / CHoCH | a bar **closes** beyond the latest unbroken swing high (low). It is a **BOS** if the structure was already in that direction (or undefined), a **CHoCH** if it was the other way. A wick through the level is not a break. |
| Structure bias | +1 after an up-break, -1 after a down-break, 0 before the first break |
| Fair value gap | candles j-2, j-1, j with `low[j] > high[j-2]`; the gap is `[high[j-2], low[j]]` (long side only). Known at the close of j. |
| Order block | when an up-break happens, the candle with the **lowest low since the broken swing high** (origin of the leg). Zone = its `[low, high]` (or body). |
| Premium / discount | dealing range = latest swing low to the highest high since; close at or below the midpoint = discount |
| Retest | the **first** bar after the zone formed whose low trades into it. A zone is used once. |
| Liquidity sweep | a bar's low pierces a still-untouched swing low (or the lowest low of the last `range_n` bars) and it **closes back above** that level, with a lower wick. |

* `smc_fvg`: buy the retest of a bullish FVG that was left by a real up-candle (`min_disp_atr`), formed in bullish structure, still bullish at entry.
* `smc_order_block`: buy the retest of the bullish order block of a structure break (`mode`: `bos`, `choch`, `any`; `min_leg_atr` filters weak impulses; `require_fvg` demands a gap in the impulse too).
* `smc_sweep`: buy the close back above a swept swing low / N-bar low; stop under the sweep low (`bias`: `any`, `not_bearish`, `bullish`).
* **Entry** (`entry_mode`): `confirm` (default) waits up to `confirm_bars` bars after the first touch for a **close back above the zone's top**; `touch` enters on the first-touch bar if it closes up.
  A close below the zone's bottom spends the zone. The stop sits `stop_buf_atr` ATRs under the lower of the zone bottom and the lowest low since the touch, never closer than `min_risk_atr` ATRs; target = `rr` x risk.
* All filters are parameters, so the platform's customise/track-record/"proven" machinery treats each variant as its own strategy.

**What this is not.** No evidence that any of it has an edge: on random data they trade and lose like anything else, and real
results depend on the market. They go through the same backtest, benchmark, and paper-trading checks as the rest, and rank as
"unproven" until they pass the gate. Not included: shorts (the platform is long-only and delivery CNC cannot short), breaker/mitigation
blocks, inducement, equal-highs/lows pools, session/kill-zone timing and higher-timeframe bias (needs intraday data; this
is daily bars). Entries use the signal bar's close and fill at the next open, so a gap past the plan can fail the risk engine's
reward:risk check (`RR_TOO_LOW`): tight-stop setups like these hit it often.

**Customise**: Backtests page, *Customise this strategy's settings*. Change the numbers, name them, save. Saved settings
are scanned alongside the built-in ones and get their own track record. `GET/POST/DELETE /strategy-configs`.

**Add a strategy**: create `tradelens/strategies/my_strategy.py` with a `Strategy` subclass (`meta`, `prepare`, `on_bar`).
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

## Decision support: gate, lifecycle, context, data quality, events
A signal is always recorded and shown. These pieces decide what happens next and tell a human what to doubt.

**The gate** (`GATE_MODE`, default `auto`). May this signal become an order? Reasons, in plain words on the Signals page:
not switched on (lifecycle), not validated on this stock, validation too old (`VALIDATION_MAX_AGE_DAYS`), results or an event
within `EVENT_WINDOW_DAYS`, and optionally the market regime is wrong (`ENFORCE_REGIME=1`). `enforce` holds the signal back
(status "Held back", no order); `advisory` creates the order and shows the warning; `auto` enforces only in `auto` trading mode,
where nobody approves. The gate runs only on the live path: backtests and portfolio backtests never call it, because a gate that
blocked backtests could never collect the evidence it asks for (a test pins this).

**Strategy lifecycle** (Strategies page). `draft` (a new or edited setting: backtest only) -> `validated` (a backtest passed the
stability check on at least one stock; set automatically; scanned and screened) -> `active` (you switched it on; only active
strategies may create orders) -> `retired` (never scanned). Evidence moves a setting forward, only a human makes it active. The
built-in strategies, and presets saved before this existed, start active.

**Market context** (bar on every page, `GET /context`). Trend (benchmark against its 200- and 50-day averages), volatility
(20-day realised volatility against the past year) and breadth (share of stored stocks above their 50-day average), from plain rules
and prices up to the decision bar only. Needs the benchmark stored: `python -m tradelens fetch --symbols NIFTYBEES`. Each strategy
declares the trends it is made for; a signal outside them is flagged `OFF_REGIME`. Every signal stores the regime it fired in.

**Data quality on every signal.** Stale prices (behind the rest of the universe), a one-day move over 35%, gaps in the data,
zero-volume days, thin trading. Level `bad` (stale, or a recent jump) means no order is ever opened from that signal, whatever the
gate says; `warn` is shown next to the signal.

**Event calendar** (Data page, `import-events`, `fetch-events`). Dated events that make a new trade riskier. CSV `symbol,date,kind[,note]`
(`*` = whole market). `fetch-events` pulls earnings dates from Yahoo, best effort: Yahoo's NSE calendar is patchy, so an empty calendar
is not proof of no events.

**Watchlist and strategy advice.** After each scan, setups one step from triggering are saved with the price that would complete them
(Screener page; the trigger levels are verified to fire the strategy exactly). For positions you hold, each strategy says EXIT or
REDUCE when the idea behind the entry stops working. This is advice only: stops and targets still close positions by themselves and
nothing is ever sold from an advice. Both are included in the daily summary alert.

**Circuit breaker** for Yahoo (history and live), Telegram and webhooks: three failures in a row pause the service for five minutes,
then one probe decides. "No data for this symbol" is a normal answer and never trips it. State is shown on the Screener page.

**Upgrading an existing database.** Missing columns are added automatically at start (`ADD COLUMN` only, nothing dropped or rewritten).
Renames or removals still need a manual migration.

## Modes (`TRADELENS_MODE`)
`signal_only` records signals · `semi_auto` creates orders that wait for your approval · `auto` sends
risk-approved orders to the broker. Stops/targets are always automatic via `POST /exits/check`.

## Portfolio backtest, benchmark, survivorship

The single-symbol backtest answers "does this rule work on this stock". The portfolio backtest answers
"would this account have made money": all symbols and strategies share ONE account, compete for the position
slots and capital of the same risk engine, and are judged against the market.

```
tradelens fetch --symbols NIFTYBEES              # the benchmark (a Nifty 50 ETF), stored like any symbol
tradelens portfolio --benchmark NIFTYBEES --start 2021-01-01 --risk-free 0.065
tradelens portfolio --liquid-top-n 15 --universe-file universe.csv
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
python -m tradelens token          # -> put it in .env as API_TOKEN (docker compose refuses to start without one)
python -m tradelens test-alert     # after setting TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (and/or ALERT_WEBHOOK_URL)
python -m tradelens run-daily      # one full pass by hand; `worker` does this on a schedule
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

## Paper trading that can be compared with the backtest

`PAPER_FILL=next_open` (the default for the API/worker; demo mode always fills at once) makes paper **entries** wait and fill at the
OPEN of the session after the signal, plus slippage: the backtest's assumption. Approving an order now returns `SUBMITTED`; the next daily
run collects the fill (`POST /orders/sync` does it on demand). Exits fill at the triggered price, as in the backtest. The pending state is
encoded in the order id, so the API process and the worker process can each answer for orders the other placed.

The daily job writes one equity snapshot per trading day. After a few weeks:

```
python -m tradelens review            # or GET /paper-review
```
It replays the same strategies over the same days and checks four things: the replay's signals appear live, fills match the model
(median entry gap), the daily job ran, and the equity curves stay close. It also lists missing signals with a likely cause.

**What 4-8 weeks can and cannot show.** It shows whether the system behaves like its backtest (implementation fidelity). It cannot show
profit: a handful of trades gives a win-rate interval like 31%-86%, which the report prints so nobody mistakes luck for an edge. Judge the
edge from the long backtest. Live sizing differs slightly from the replay on purpose: the order is sized before the open is known, the
replay sizes at the actual fill (in a 70-day simulation this alone produced a 0.8pp equity gap with identical trades and exits).

## Going live with Zerodha (read all of it)

The adapter exists and is tested against a fake Kite client and the real SDK's method signatures. It has **never talked to Zerodha**:
there is no sandbox, so the first real order is also the first real test. Plan for that.

Prerequisites (from Zerodha's current docs; re-check them):
1. A Kite Connect app (the free "Personal" plan has orders and GTT but no market data; data stays on yfinance here).
2. A **static IP** registered in the developer console. Orders from any other IP are rejected. It must equal your server's exact
   public egress IP (IPv4 vs IPv6 mismatches are a known trap). Changes are limited to once a week.
3. **Exchange-side stops use GTT**, which places a LIMIT order when triggered. A gap through the limit leaves the shares unsold, and selling
   held shares can need CDSL TPIN authorisation or a pre-authorisation. Check that on your account first. tradelens reports a triggered
   but unfilled stop as a critical alert and keeps its own software stop as a second line; neither is a guarantee.
4. Daily login: `python -m tradelens kite-login` each trading morning (tokens expire around 06:00 IST). A missing session raises a critical alert.

Order of events, each only after the previous one is boring:
```
BROKER=zerodha ZERODHA_DRY_RUN=1     # everything runs, no order is sent; rejected orders say what they WOULD have placed
python -m tradelens kite-check       # session, funds, caps; places nothing
ZERODHA_DRY_RUN=0 TRADELENS_MODE=semi_auto LIVE_MAX_ORDER_VALUE=<one small position>
```
Safety rails that do not depend on the risk engine: per-order and per-day BUY value caps counted from Zerodha's own order book, limit
orders only (a marketable-limit cushion of `LIVE_LIMIT_BUFFER_BPS`, so no market-protection setting is needed), `TRADELENS_MODE=auto`
refused unless `LIVE_ALLOW_AUTO=1`, orders idempotent on their tag, and a timeout checks the order book by tag before saying anything.
If it cannot tell whether an order went out, the order is marked `UNCERTAIN`, you get a critical alert, and nothing is retried.
After-hours orders go in as AMO; the session-window rule is a guess to verify on your first day. After every daily run, positions are
reconciled with the broker (it alarms when the broker holds LESS than you think, never "fixes" anything).

## Signal filters (news / events / AI): shadow first

A filter can only veto a trade the risk engine already approved. `EVENT_BLACKOUT_DAYS` + `EVENTS_FILE` (a CSV you maintain:
`symbol,date,kind`) skips signals whose position would be open through a results date or ex-dividend. With `FILTERS_ENFORCED=0` it only logs
(table `filter_log`); the review compares vetoed vs allowed trades. Enforce a filter only after that comparison shows it helps, on far more
than a few trades. An LLM/news filter should be added the same way, as one more shadow filter; none is included, because there is no
free, reliable news feed to build it against.

## Guarantees the tests enforce
Fills at next bar open (no look-ahead) · stop beats target inside one bar · gaps fill at the open ·
costs on both legs · backtest and live share the same RiskEngine and exit rule · re-scans never
duplicate orders · stale signals are rejected at approval · kill switch blocks everything.

## Known limits (be honest with yourself)
Daily bars, long-biased strategies, single account, equity-style costs (verify the rates against
Zerodha's calculator), single shared-token auth (no user accounts), no options/forex data yet, new database columns are added automatically at start, but renames or removals still need a manual migration (no Alembic yet).

Portfolio backtest limits: default strategy parameters only (saved presets are not used yet); the daily loss limit
uses the previous trading day's realized P&L (daily bars); positions in symbols whose data ends are closed at the last
close, which flatters a collapse; parameter/strategy selection across many runs is not corrected for luck.
