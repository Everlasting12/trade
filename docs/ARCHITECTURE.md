# Algo Trading Platform: Architecture and Technical Design

Indian-market F&O algorithmic trading platform on Zerodha Kite Connect, with a Python/FastAPI
backend and a Streamlit dashboard.

Design priority, in order: **correctness → risk management → reliability → testability → performance → UI**.

> No strategy in this repository is assumed to be profitable. Every strategy must be backtested on real
> data, then paper-traded, before it is allowed anywhere near live capital.

---

## 1. Requirements analysis

The spec is sound. Its central principle is "same strategy code in backtest, paper and live; only the
execution adapter changes", and that principle drives the whole design. The most important
consequence is that the **engine cannot live inside Streamlit**. Streamlit re-executes scripts on every
interaction and per browser session. Trading needs one long-lived, single-owner process.

### Missing requirements and gaps found while building

| Area | Gap / problem | How it is handled now |
|---|---|---|
| Regulation | The SEBI retail-algo framework applies to API trading: static-IP registration, algo-ID tagging and order-rate thresholds. It must be confirmed with Zerodha before going live. | Documented as a live-mode prerequisite. Not automatable. |
| Daily login | Kite access tokens expire daily. Automating the login (password/TOTP) is not allowed. | A human logs in each morning through the OAuth flow. The token is cached for the day only. |
| Exchange calendar | Holidays, special sessions and Muhurat trading are not modelled. Only weekends are known. | **TODO**: load the NSE holiday calendar. Stale-data checks prevent trading on a dead feed. |
| Freeze quantity | NSE rejects orders above the freeze quantity, so large orders must be sliced. | `max_position_lots` and `LIVE_MAX_ORDER_LOTS` cap size. **TODO**: order slicing. |
| Rate limits | Kite limits orders per second, minute and day, and historical calls (3/s). | Historical calls are throttled. **TODO**: an order-rate limiter in the OMS. |
| Historical data | The historical API is a paid add-on. Expired contracts have no intraday history, and continuous data is daily only. Option history is effectively unavailable. | Multi-year F&O intraday backtests need a data vendor. CSV import is provided. |
| Index volume | Indices have no volume, so VWAP and volume factors are meaningless on the spot index. | Strategies trade the near **future**. The scorer marks volume/VWAP as *not applicable* when absent, so they don't count against the score. |
| Spec example limits | `MAX_RISK_PER_TRADE=500` is below the risk of a single NIFTY futures lot (75 × a ~20-point stop ≈ ₹1,500). Every signal would be rejected. | Defaults are sized to ~1% of ₹5L capital per trade and 3% per day. The risk manager explains each rejection. |
| Margin | SPAN+exposure margin can't be computed locally. | Live mode uses Kite's `order_margins`. Paper mode uses a configurable percentage approximation. |
| Multiple strategies on one instrument | The broker nets positions per instrument, so two strategies would fight over one net position. | Rule: **one open position per instrument**, owned by one strategy. |
| Stock F&O settlement | Stock derivatives are physically settled at expiry. | **TODO**: forced exit before expiry week, and rollover rules. |
| Charges | STT and exchange fees change with budgets and circulars. | All rates are configurable (`execution/charges.py`). **Verify against zerodha.com/charges.** |
| Options execution | "Signal on the underlying, trade the option" needs stops referenced to the underlying, not to the premium. | The contract selector, option chain and greeks exist. Underlying-referenced option execution is Phase 9. |

---

## 2. System architecture

```
┌──────────────────────── Backend process (FastAPI, 127.0.0.1:8000) ────────────────────────┐
│                                                                                            │
│  Feed thread(s)            Engine worker thread (single owner of all trading state)        │
│  KiteTicker / Synthetic ─► queue ─► TradingEngine                                          │
│  (ticks, order postbacks)            ├─ MultiTimeframeAggregator (tick → 1m → 3m…1d)       │
│                                      ├─ IndicatorEngine (incremental)                      │
│  API threads ── submit(fn) ────────► ├─ Strategies → SignalScorer → RiskManager            │
│  (commands)   ◄─ Future result ───── ├─ OrderManager (OMS) → BrokerInterface               │
│               ◄─ snapshot (1 s) ──── │                        ├─ PaperBroker               │
│                                      │                        └─ ZerodhaBroker + Guard     │
│                                      └─ Portfolio, EventLog                                │
│  SQLite (WAL): orders, fills, signals, trades, events, candles, state                     │
└────────────────────────────────────────────────────────────────────────────────────────────┘
                 ▲ HTTP (JSON)
┌────────────────┴──────────── Streamlit process (localhost:8501) ──────────────────────────┐
│  Pure client: pages poll the snapshot via fragments; commands are POSTs. No broker objects. │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Code layout

| Package | Responsibility |
|---|---|
| `app/core` | Domain models (`Instrument`, `Tick`, `Candle`, `Signal`, `Order`, `Fill`, …) and the IST clock. |
| `app/market_data` | Timeframes and session bucketing, tick → candle aggregation, the instrument master, historical data, and the live feeds. |
| `app/indicators` | Incremental indicator framework, the indicator library, and the per-instrument engine. |
| `app/patterns` | Candlestick patterns, price action (swings, structure, S/R, breakouts) and chart patterns. |
| `app/strategies` | The `Strategy` interface and `StrategyContext`, plus 6 reference strategies and a registry. |
| `app/signals` | Configurable signal scoring. |
| `app/risk` | Risk manager, position sizing, kill switch and daily halts. |
| `app/orders` | The OMS: brackets, OCO, idempotency and reconciliation. |
| `app/execution` | The `BrokerInterface`, the charges model, `paper/` and `zerodha/` (session, guard, broker). |
| `app/portfolio` | Positions, trades, equity and performance metrics. |
| `app/backtesting` | The event-driven backtester, built on `TradingEngine`. |
| `app/engine` | `TradingEngine` (the shared pipeline) and `EngineRuntime` (threads, persistence, recovery). |
| `app/fno` | Black-Scholes IV and greeks, option chain, PCR, max pain and contract selection. |
| `app/options` | Options automation: option market (Kite quotes or a Black-76 model) and the planner (BUY/SELL × CE/PE). See §16. |
| `app/database` | SQLite repository. |
| `app/utils` | Structured event log. |
| `backend/` | FastAPI app and routers: auth, account (read-only), market, engine, backtest. |
| `frontend/` | Streamlit `st.navigation` app with one page per dashboard section. |
| `config/trading.yaml` | Strategies, scoring, risk defaults and the paper fill model. |
| `tests/` | 74 tests: indicators vs. pandas references, candles, broker, OMS, risk, backtest look-ahead, live safety, F&O, options planner, option backtests, and the journal. |

---

## 3. Data flow

1. **Tick** (KiteTicker, FULL mode) goes onto the queue, then the engine worker.
2. **Paper only:** the `PaperBroker.on_quote` call fills working orders against the bid/ask and the top-of-book quantity.
3. **Portfolio mark-to-market:** unrealized P&L, MFE/MAE.
4. **Candles:** the aggregator builds every subscribed timeframe from ticks.
   - It drops duplicate ticks and out-of-order or late ticks.
   - It computes volume from the difference in cumulative volume.
   - A candle closes when the next bucket starts, or on the 1-second timer after its end plus a grace period.
5. **On candle close:** indicators update incrementally (O(1) or O(period)). Then each subscribed, enabled, warmed-up strategy runs with a read-only `StrategyContext` that contains only *closed* candles.
6. **Signal:** `SignalScorer` scores it 0–100 against configurable factors and ignores it below `min_score`.
7. **Risk:** `RiskManager` runs every check and sizes the position from the stop distance and margin. Its `RiskDecision` lists each check with its outcome.
8. **Live only:** the `LiveTradingGuard` gate.
9. **OMS:** places the entry, then a bracket (SL-M stop + LIMIT target, OCO) on each fill.
10. **Broker:** `PaperBroker` or `ZerodhaBroker`. Updates come back as `OrderUpdate`s, through the OMS, into the `Portfolio` (positions and round-trip trades).
11. **Persistence:** every step is logged to `EventLog` (JSON lines + SQLite). Orders, fills, signals and trades are persisted. Recovery state is saved every 5 seconds.

Backtests drive the same `TradingEngine.on_bar()`. The PaperBroker fills on the bar's OHLC first, then the candle closes and strategies run, so an order created on bar *t* can only fill on bar *t+1*.

---

## 4. Libraries and technologies

| Need | Choice | Why |
|---|---|---|
| Broker API and WebSocket | `kiteconnect` (official) | Official SDK; KiteTicker handles reconnects. |
| Backend API | FastAPI + uvicorn | Typed request models, threadpool for sync endpoints, OpenAPI docs at `/docs`. |
| UI | Streamlit (`st.navigation`, fragments) | As required. Fragments give partial auto-refresh. |
| Numerics | Plain Python incremental math, plus numpy/pandas for batch and analysis | Incremental math gives identical live/backtest values with no per-tick recomputation. |
| Charts | Plotly (candlesticks + subplots), Streamlit native charts | |
| Storage | SQLite (WAL) | Zero-ops for a single process. Production: PostgreSQL + TimescaleDB. |
| Config | `.env` (python-dotenv) for secrets and switches; YAML for trading parameters | |
| Packaging and tests | uv, pytest | |

Deliberately **not** used yet:
- TA-Lib, because of its C dependency. Our conventions match it, and that is tested.
- An async engine. One worker thread is simpler and easier to make correct.
- Kafka or Redis. Not needed for one process. Revisit only when running several engine processes.

---

## 5. Database schema

The DDL is in `app/database/repository.py`. Every table except `candles` carries a `mode` (paper/live) column. Backtests are not persisted by default.

| Table | Key | Contents |
|---|---|---|
| `orders` | `client_order_id` | Broker id, strategy, instrument, side, quantity, type, product, role (ENTRY / STOP_LOSS / TARGET / EXIT), price, trigger, status, filled quantity, average price, parent id, signal id, reject reason, timestamps. Upserted on every change. |
| `fills` | `fill_id` | Order id, quantity, price, charges, role, timestamp. |
| `signals` | `signal_id` | Strategy, instrument, timeframe, action, entry/SL/target, quantity, score, grade, **status** (`approved` / `risk_rejected` / `ignored_low_score`), plus JSON reasons, risk decision and score breakdown. |
| `trades` | `trade_id` | Closed round trips: entry/exit price and time, gross P&L, charges, net P&L, exit reason. |
| `events` | autoincrement | Structured audit log: ts, level, event, strategy, instrument, action, reason, order id, position id, error, JSON data. |
| `candles` | (token, timeframe, ts) | Historical candle cache. |
| `watchlists`, `watchlist_items` | id / (list, instrument) | The user's own watchlists, shared by paper and live. Kite Connect has no watchlist API. |
| `state` | `mode:key` | JSON recovery state: portfolio, OMS brackets and idempotency keys, paper account, risk/kill switch, strategy toggles. |

---

## 6. Broker abstraction

```python
class BrokerInterface(ABC):
    def set_update_handler(handler)          # pushes OrderUpdate (status, cumulative + incremental fills, charges)
    def place_order(order) -> broker_id      # NEVER retried by callers
    def modify_order(order, *, quantity, price, trigger_price, order_type)
    def cancel_order(order)
    def get_order_status(order) / get_orders() / get_positions() / get_account_balance()
    def estimate_margin(order, instrument, price)
```

- **PaperBroker** and **ZerodhaBroker** implement it. The OMS is the only caller, and strategies never see it.
- Both brokers report lifecycle events through the same `OrderUpdate` stream. The OMS therefore has a single code path for partial fills, rejections and cancels.
- `OrderRejected` means a definite rejection. A bare `BrokerError` means the outcome is ambiguous. The OMS marks such an order `UNKNOWN` and reconciles it from the broker's order book using the order `tag` (which equals the client order id). It never re-sends.
- Adding a new broker means implementing this interface plus an order-update translator.

## 7. Strategy interface

```python
class MyStrategy(Strategy):
    name = "my_strategy"
    default_params = {"fast": 20, "slow": 50, "atr_period": 14, "atr_mult": 1.5, "rr": 2.0}

    def indicators(self):                        # declared up front; registered + warmed up by the engine
        return [self.spec("ema", period=20), self.spec("ema", period=50), self.spec("atr", period=14)]

    def on_candle(self, ctx: StrategyContext):   # called on each CLOSED candle of self.timeframe
        if ctx.position_qty == 0 and ctx.crossed_above(("ema", {"period": 20}), ("ema", {"period": 50})):
            c, atr = ctx.candle, ctx.ind("atr", period=14)
            return ctx.signal("BUY", stop_loss=c.close - 1.5 * atr, target=c.close + 3 * atr,
                              reason=["EMA20 crossed above EMA50"])
```

`StrategyContext` gives the strategy:
- closed candles for any subscribed timeframe, including higher-timeframe confirmation
- indicator values with offsets, plus `crossed_above` / `crossed_below`
- lazily computed `price_action()`, `candle_patterns()` and `chart_patterns()`
- its own `position`, and an option-chain hook

A strategy returns `Signal`s and nothing else.

- **Custom strategies:** subclass `Strategy` and decorate it with `@register`.
- **Configuration:** strategies are configured in YAML (id, type, instruments, timeframe, params, enabled).
- **Error handling:** a strategy that raises is logged, and after 5 consecutive errors it is auto-disabled.

## 8. Paper-trading execution engine

`PaperBroker` simulates the exchange locally and never calls Zerodha.

- **Orders:** MARKET, LIMIT, SL (stop-limit), SL-M, and DAY expiry. Orders placed at or after the session close survive as AMOs.
- **Tick mode (paper):** market orders fill at the ask/bid plus adverse slippage (in bps). Partial fills are limited by top-of-book quantity. Limit orders fill when marketable, at the better price. Stops trigger on LTP.
- **Bar mode (backtest):**
  - Fills happen on the *next* bar, at its open.
  - Gaps fill at the open: worse for stops, better for limits.
  - **When a stop and a target both fall inside one bar, the stop is assumed to hit first** (pessimistic).
  - Partial fills are capped at a share of the bar's volume.
- **Validation:** lot-size multiples, tick size, triggers that have already been crossed, index not tradable, insufficient margin.
- **Accounting:** cash, realized P&L, mark-to-market and used margin (futures %, option premium, short options on strike notional).
- **Charges:** brokerage capped per *order* across partial fills, STT, exchange fees, SEBI fee, stamp duty and GST (`ChargesModel`).
- **Persistence:** state is saved via `to_state()` / `load_state()` for restart recovery.

Because the OMS, risk manager and strategies are identical across modes, a paper result differs from live only by this fill model.

## 9. Safe Zerodha integration

There are **six independent layers**, and all of them must pass:

1. `LIVE_TRADING_ENABLED=true` (env; default `false`; needs a restart to change).
2. `TRADING_MODE=live` (env). If only one of the two is set, the engine **refuses to start** rather than silently falling back.
3. Startup **reconciliation**: the broker's positions and orders must match internal state, or the operator must explicitly acknowledge the mismatches. Reconciliation re-runs every 60 seconds and **auto-disarms** on a mismatch.
4. **Runtime arming** in the UI, by typing the exact phrase "I UNDERSTAND THIS PLACES REAL ORDERS" plus an operator name. Arming expires at the end of the day and is cleared by a restart or the kill switch.
5. The kill switch must be off.
6. A hard **per-order lot cap** (`LIVE_MAX_ORDER_LOTS`, default 1).

The engine checks the guard before risk approval. `ZerodhaBroker.place_order` checks it **again** as the last line of defence. `tests/test_live_safety.py` proves no order reaches Kite unless every layer passes.

Other live details:
- MARKET and SL-M orders carry `market_protection=-1`.
- Order updates arrive via WebSocket postbacks **and** a 5-second order-book poll. They are de-duplicated, and stale or out-of-order updates are dropped.
- Reads, cancels and modifies are retried with backoff. `place_order` is never retried.
- Secrets live only in the environment. The access token is cached locally for the day. The API binds to `127.0.0.1` because it has no authentication of its own.
- **Production:** put the API behind an authenticated reverse proxy and inject secrets from a secret manager.

## 10. Streamlit UI architecture

- `frontend/streamlit_app.py` sets up `st.navigation` with three sections. Trade has Trading desk, Watchlists, Option chain and Trade journal. A compact live watchlist sits in the sidebar. Build & test has Strategies and Backtest. Settings has Risk & safety, Activity log and Zerodha account. A status bar on every page shows paper/live, data freshness, the auto-trading switch and the emergency stop. Charts use TradingView Lightweight Charts (`frontend/charts.py`).
- A **mode banner** on every page, refreshed every 3 seconds: a green "🟢 PAPER TRADING" alert, or a red "🔴 LIVE TRADING" error showing the armed state. The kill-switch and daily-halt banners also appear on every page.
- Live sections are `@st.fragment(run_every=…)` blocks that read the backend snapshot. Controls (kill switch, arming, manual order, exits) are forms with confirmations.
- The UI holds **no trading state** and no broker objects. Everything goes over HTTP.

## 11. Concurrency issues Streamlit introduces, and how they are avoided

| Streamlit behaviour | Risk if the engine lived in Streamlit | Mitigation |
|---|---|---|
| The script re-runs on every interaction | Duplicate engines, feeds and orders | The engine lives in the backend process. The UI is a client. |
| One session per browser tab | N tabs would mean N engines | A single engine per backend process. |
| Background threads aren't tied to the session lifecycle | Orphaned threads, `ScriptRunContext` errors | Feed and engine threads are owned by `EngineRuntime`. |
| Reruns can interrupt mid-script | A half-sent order | Orders are sent only on the engine thread, never from UI code. |
| `st.cache_resource` is shared across sessions | Accidental shared mutable state | Not used for trading state. |

Inside the backend: KiteTicker callbacks run on Twisted's reactor thread, and FastAPI runs endpoints in a threadpool. Neither touches engine state. They enqueue work (ticks, postbacks, `submit(fn)` commands), and **one worker thread** executes it sequentially. Reads come from an immutable snapshot republished every second. The engine itself therefore needs no locks.

## 12. Consuming live data without blocking the UI

- Tick callbacks only call `queue.put_nowait`, which never blocks the socket thread.
- The worker drains the queue. A 1-second timer flushes candles, runs square-off, risk housekeeping and order polls, persists state, and publishes the snapshot.
- The UI polls `/engine/snapshot` in fragments: the overview every 2 seconds, other pages every 2–3 seconds. A slow UI cannot back-pressure the engine.
- Slow work (instrument dump, historical warmup) runs on a separate start-up thread, so the API serves immediately.

## 13. Logging, error handling, recovery and the kill switch

- **Structured events:** every action is recorded: signal generated, score ignored, risk passed or rejected, order submitted/filled/rejected/cancelled, position opened/closed, strategy error, broker API error, feed status, reconciliation, kill switch. Each record has ts, mode, level, event, strategy, instrument, action, reason, order_id, position_id, error and data. They go to a rotating `logs/events.jsonl` file, the SQLite `events` table and an in-memory buffer for the UI.
- **Error handling:**
  - A strategy exception is logged and the strategy auto-disabled after 5 in a row.
  - A broker read failure is retried. A send failure becomes `UNKNOWN` and is reconciled.
  - A rejected protective stop causes an immediate market exit. The OMS never leaves a position unprotected.
  - A rejected exit is logged at CRITICAL level.
  - Stale data (a tick older than 15 seconds, wall-clock) blocks entries.
  - Duplicate or late ticks are dropped.
  - Day orders that expired overnight have their protection re-armed when the next session starts.
- **Recovery:** portfolio, OMS (brackets and idempotency keys), paper account, risk/kill switch and strategy toggles are persisted every 5 seconds and on every command. On restart they are restored, and live mode reconciles with the broker before arming is allowed.
- **Kill switch:** global and persisted across restarts. It blocks entries, cancels working orders, optionally flattens everything, and disarms live trading. Exits remain allowed. It trips automatically at the portfolio drawdown limit.
- **Daily halt:** when daily loss or consecutive losses breach their limits, new entries stop for the day, optionally with a flatten.

## 14. MVP scope and roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Market data + dashboard | ✅ KiteTicker feed, tick→candle aggregation (1m–1d), quotes, watchlist, Streamlit dashboard |
| 2 | Historical data + indicators | ✅ Kite historical (chunked, cached), CSV import, 14 incremental indicators |
| 3 | Strategy engine + signals | ✅ Strategy interface, 6 strategies, patterns and price action, configurable scoring |
| 4 | Backtesting | ✅ Same engine, no look-ahead (tested), all requested metrics |
| 5 | Paper trading | ✅ PaperBroker (slippage, partial fills, charges, margin), synthetic session mode |
| 6 | Risk + portfolio | ✅ All requested limits, sizing, kill switch, daily halt, strategy-level limits |
| 7 | Zerodha integration | ✅ Adapter, postbacks + polling, reconciliation. **Not yet exercised against a real account.** |
| 8 | Live trading with strict controls | 🟡 Six-layer guard, tested with a fake Kite. Needs supervised small-size trials, an order-rate limiter, a holiday calendar, freeze-qty slicing, and SEBI algo compliance. |
| 9 | Advanced F&O | ✅ Options automation (§16): BUY/SELL × CE/PE planner, delta-based strikes, expiry rules, underlying-referenced exits, stop-limit option stops with a watchdog, expiry-day exit, trade journal. 🟡 Remaining: multi-leg (spreads), rollover, stock-option physical-settlement rules. |
| 10 | AI-assisted analysis | ⬜ Design: AI produces a structured `Signal` that flows through the same scorer, risk manager and OMS. It never calls a broker. |

### Recommended path to live

1. Real-data backtests: get the historical add-on or a vendor dataset.
2. At least 4–8 weeks of paper trading with a stable strategy configuration. Compare paper fills with what the live market would have given.
3. Live with `LIVE_MAX_ORDER_LOTS=1` and minimal limits, supervised.
4. Scale only after paper and live results agree.

## 15. Key risks and trade-offs

- **Bar-based backtests can't know the intrabar path.** The pessimistic stop-first rule understates results that rely on wide bars. Use 1-minute base data.
- **Paper fills are optimistic in thin markets.** Top-of-book partial fills help, but there is no queue-position model. Limit orders filling "on touch" is configurable.
- **The equity-curve Sharpe on intraday strategies** uses daily returns with rf=0. Treat it as relative, not absolute.
- **One process means one point of failure.** This is acceptable at retail scale. Recovery state plus broker reconciliation covers restarts. For high availability, run a supervised process (systemd/NSSM) with alerting.
- **SQLite** is ideal for a single writer. Move to PostgreSQL when multiple processes or remote dashboards are needed.
- **Synthetic mode** exists only to exercise the plumbing outside market hours. Its P&L means nothing.

## 16. Options automation

The platform's primary use is automated index and stock **options** trading.

```
strategy (on the underlying FUTURE) ── bullish/bearish view + underlying stop/target
   │
   ▼ scorer (0-100) ── skip weak views
   ▼ OptionsPlanner
        regime:  ADX (trend strength) · ATM IV vs realised volatility (are options expensive?)
        action:  bullish → BUY_CE (trending, IV not rich) | SELL_PE (ranging or IV rich)
                 bearish → BUY_PE                          | SELL_CE
        expiry:  nearest with ≥ min_dte (buy 1.0 d, sell 0.5 d by default)
        strike:  |delta| ≈ 0.50 for buys, ≈ 0.25 for sells; spread/premium/OI filters
        levels:  premium stop/target = underlying stop/target × delta (+ ½·gamma·move²), capped
   ▼ RiskManager (premium-based sizing, option caps, option selling on/off)
   ▼ OMS: entry + stop-limit SL (SL-M not allowed on options) + target, OCO
   ▼ exits: premium SL/target · underlying SL/target · strategy exit · square-off · expiry-day exit · watchdog
   ▼ journal row per trade (entry/exit, planned R:R, actual R, IV, delta, reasons, P&L)
```

- **Option market** (`app/options/market.py`). `KiteOptionMarket` fetches real chains with `kite.quote`. It
  computes the forward from put-call parity, then IV and greeks with Black-76. `ModelOptionMarket` prices
  chains and held contracts from the underlying, and is used for synthetic sessions and backtests.
- **Contract subscription.** When the planner picks a contract, the engine starts streaming it. The chain
  quote seeds the price, so the paper broker can fill immediately.
- **Strategies stay underlying-centric.** A strategy sees its option trade as a long (bullish) or short
  (bearish) position on the underlying, so the same strategy code works for futures or options execution.
- **Paper margin.** When logged in, paper margin comes from Kite's `order_margins` (real SPAN + exposure).
- **Limitations.**
  - Option backtests use modelled premiums (fixed IV plus a smile), so treat them as logic tests.
  - Single-leg trades only; no spreads or hedges yet.
  - Change in OI uses an intraday baseline.
  - Short options carry gap risk beyond the stop, which is why `max_short_option_lots` defaults to 1.
