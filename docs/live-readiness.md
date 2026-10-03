# tradelite Paper-Trading Protocol (8-12 weeks)

## Overview

This protocol is designed to collect real evidence before any real money is involved.
The system runs in `semi_auto` mode with the worker active, recording all trades,
rejections, slippage, and regime data.

## Protocol Phases

### Phase 1: Initialization (Weeks 1-2)
- [ ] Set `TRADELITE_DEMO=0` and ensure real NSE data is loaded (`REFRESH_YEARS=15`)
- [ ] Set `API_TOKEN` and configure `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` for alerts
- [ ] Set `POSTGRES_PASSWORD` in `.env`
- [ ] Verify `docker compose up` starts successfully with required tokens
- [ ] Run `python -m tradelite token` and save the token to `.env`
- [ ] Set `HOLIDAYS_FILE` and place `data/holidays.txt`
- [ ] Verify worker uptime > 99% over first 5 trading days

### Phase 2: Data Quality Verification (Weeks 2-4)
- [ ] Verify no `missing_price_data` flags in the report for well-known NSE symbols
- [ ] Verify `bias_risk` is not `HIGH` when universe file is loaded
- [ ] Check that data quality flags are reasonable (some gaps/staleness expected)
- [ ] Confirm slippage assumptions are realistic (compare assumed vs realized)

### Phase 3: Strategy Validation (Weeks 4-8)
- [ ] Run the platform with default strategies on a universe of 20-30 liquid NSE stocks
- [ ] Generate weekly reports: `tradelite report --weeks N`
- [ ] Track these metrics:
  - Trades count and breakdown by strategy/symbol
  - Realised vs expected R (interval low above 0 is a positive sign)
  - Slippage within 25% of assumed
  - No unexplained rejections by the risk engine
  - Worker uptime ≥ 99%
  - Drawdown inside the backtest's 95% band
  - Model status: validated/rejected as per gate criteria
- [ ] If the strategy set passes all criteria, move to Phase 4
- [ ] If not, review the report, adjust assumptions, or reduce universe

### Phase 4: Final Decision (Weeks 8-12)
- [ ] **Go criteria** (all must be met):
  - Enough trades to judge (at least 30-50 real trades)
  - Realised slippage within 25% of assumed slippage
  - No unexplained rejections by the risk engine
  - Worker uptime of at least 99%
  - Drawdown inside the backtest's 95% band
  - Model validated (calibration slope within 0.8-1.2, AUC > 0.55)
- [ ] **No-go criteria** (any one triggers stop):
  - Realised slippage exceeds 25% of assumed
  - Unexplained rejections by the risk engine
  - Drawdown breaches the backtest's 95% band
  - Model rejected (calibration slope outside 0.8-1.2 or AUC ≤ 0.55)
- [ ] **If Go**: Start with a very small amount placed manually in the broker's own app,
  not through an API, for a further period of 2-4 weeks
- [ ] **If No-Go**: Document the decision memo, keep paper-trading, or adjust strategies

## Weekly Report Format

```
tradelite report --weeks N
```

Delivers:
- Trades list (symbol, strategy, entry, exit, P&L, r_multiple, exit reason)
- Realised vs expected R (with confidence interval)
- Slippage vs assumed (bps and % difference)
- Rejections by reason (kill switch, daily loss limit, sector limit, etc.)
- Regime distribution (how many signals in each regime)
- Model status (active/rejected/candidate)
- Data issues (missing price data, gaps, staleness)
- Worker uptime
- Edge-decay monitor (z-score of realised expectancy vs expected)

## Decision Memo Template

```
GO/NO-GO DECISION MEMO
=====================

Strategy Set: <strategy names>
Period: <start date> to <end date>
Trades: <n_trades>
Realised Slippage: <X> bps (assumed: Y bps, within 25%: YES/NO)
Drawdown: <peak-to-trough %>, within backtest 95% band: YES/NO
Worker Uptime: <X>% (required: ≥ 99%)
Model Status: <active/rejected/candidate>, calibration slope: <X.X>
Data Quality: <no issues/minor issues/major issues>

GO/NO-GO: <GO/NO-GO>

RATIONALE:
- Enough trades to judge: YES/NO
- Slippage within 25% of assumed: YES/NO
- No unexplained rejections: YES/NO
- Worker uptime ≥ 99%: YES/NO
- Drawdown inside backtest 95% band: YES/NO
- Model validated: YES/NO

NEXT STEPS:
- If GO: Manual small amount in broker app for <X> weeks
- If NO-GO: Adjust strategies, reduce universe, or continue paper-trading
```

## Go/No-Go Criteria Checklist

| Criterion | Threshold | Status |
|-----------|-----------|--------|
| Enough trades to judge | ≥ 30-50 real trades | ✓/✗ |
| Realised slippage within 25% of assumed | YES/NO | ✓/✗ |
| No unexplained rejections by risk engine | YES/NO | ✓/✗ |
| Worker uptime ≥ 99% | YES/NO | ✓/✗ |
| Drawdown inside backtest's 95% band | YES/NO | ✓/✗ |
| Model validated (calibration slope 0.8-1.2, AUC > 0.55) | YES/NO | ✓/✗ |

## Known Limits (Be Honest)

- Daily bars only, long-biased strategies
- Single shared-token auth (no user accounts)
- No options/forex data yet
- Database schema created on start (no migrations yet)
- Portfolio backtest: default strategy parameters only
- Daily loss limit uses previous trading day's realised P&L
- Positions in symbols whose data ends are closed at last close
- Parameter/strategy selection across many runs not corrected for luck
- Free Yahoo intraday data for NSE is unofficial, may be delayed

## Next Steps After Go

1. Start with a very small amount placed manually in the broker's own app
2. Not through an API, for a further period of 2-4 weeks
3. Monitor closely: track all the same metrics from the paper-trading protocol
4. If successful, gradually increase position size per risk limits
5. If unsuccessful, stop, review, and iterate the strategy