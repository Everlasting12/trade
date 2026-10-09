# Developer guide

How to set up, run, test and extend this project locally. For *why* things are designed the way
they are, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

The app has two processes:

| Process | What it does | URL |
|---|---|---|
| **Backend** (FastAPI, `backend/`) | Owns the Kite session, the trading engine (`app/`) and the SQLite database | http://127.0.0.1:8000 (API docs at `/docs`) |
| **Dashboard** (Streamlit, `frontend/`) | UI only; talks to the backend over HTTP | http://localhost:8501 |

---

## 1. Prerequisites

- **Python 3.14** (see `.python-version`)
- **uv**, the package manager used by this project (`pyproject.toml` + `uv.lock`)
- **Optional:** a Zerodha Kite Connect app (API key + secret). Without one you can still run backtests and a
  simulated paper session.

### Installing uv

**Option A (recommended): the official installer.** It also puts `uv` on your PATH.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal, then check it with `uv --version`.

**Option B: install with pip.**

```powershell
python -m pip install --user uv
```

pip installs `uv.exe` into your user Scripts folder, which is usually **not** on PATH. On this machine that is
`C:\Users\<you>\AppData\Roaming\Python\Python314\Scripts`. You then have two choices:

- Prefix every command with `python -m`, for example `python -m uv sync` or `python -m uv run pytest`.
- Or add the folder to PATH, then open a new terminal:

  ```powershell
  $scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
  [Environment]::SetEnvironmentVariable("Path", "$env:Path;$scripts", "User")
  ```

> If you see `uv : The term 'uv' is not recognized...`, uv is installed but not on PATH. Use `python -m uv ...`
> or apply the PATH fix above.

---

## 2. Install dependencies

From the **project root** (`C:\Coding\trade`):

```powershell
uv sync
```

This creates `.venv/` and installs everything, including the dev dependency `pytest`.

---

## 3. Configure

### 3.1 Environment (`.env`)

```powershell
Copy-Item .env.example .env
```

Then edit `.env`. The variables that matter:

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_API_KEY` / `BROKER_API_SECRET` | empty | From your Kite Connect app. `KITE_API_KEY` / `KITE_API_SECRET` also work. |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `LIVE_TRADING_ENABLED` | `false` | Must be `true` (and mode `live`) for real orders. **Leave it false while developing.** |
| `FEED_SOURCE` | `kite` | `kite` uses live Kite WebSocket data. `synthetic` uses a simulated session (paper only). |
| `SYNTHETIC_SPEED` | `10` | Speed multiplier for the simulated session |
| `DEFAULT_CAPITAL`, `MAX_DAILY_LOSS`, `MAX_RISK_PER_TRADE`, `MAX_TRADES_PER_DAY`, `MAX_OPEN_POSITIONS` | see file | Risk limits. These override `config/trading.yaml`. |
| `DB_PATH`, `LOG_DIR` | `data/trading.db`, `logs` | Where state and audit logs go |

`.env` is gitignored. **Never commit it.**

### 3.2 Kite Connect app (only for live data)

1. Create an app at <https://developers.kite.trade/apps>.
2. Set its **Redirect URL** to `http://127.0.0.1:8000/auth/callback`.
3. Put the API key and secret in `.env`.

### 3.3 Trading configuration

[config/trading.yaml](config/trading.yaml) holds the strategies, signal-scoring weights, risk limits and the paper
fill model. Changes apply when the engine restarts.

---

## 4. Run

> **Run every command from the project root (`C:\Coding\trade`), not from `backend/` or `frontend/`.**
> The module path `backend.main:app` and the `app` package only resolve from the root. Running from `backend/`
> gives `ModuleNotFoundError: No module named 'backend'`.

Open two terminals in the project root.

**Terminal 1: backend**

```powershell
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

**Terminal 2: dashboard**

```powershell
uv run streamlit run frontend/streamlit_app.py
```

Open http://localhost:8501 and click **Log in with Kite** in the sidebar. After the redirect, the engine starts
in **paper** mode. Startup does three things:

1. Loads the instrument master.
2. Warms up indicators from historical data. This needs the Kite historical-data add-on; without it, indicators
   warm up from live ticks.
3. Streams the instruments used by the strategies in `config/trading.yaml`.

### Without a Kite account, or outside market hours

Run a simulated paper session. It uses random-walk prices on a virtual 09:15–15:30 clock, so the P&L is
meaningless, but it exercises the full pipeline.

```powershell
$env:FEED_SOURCE = "synthetic"; $env:SYNTHETIC_SPEED = "20"
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

You can also put `FEED_SOURCE=synthetic` in `.env`. Backtests on synthetic or CSV data work in either mode, from
the **Backtest** page.

### Useful URLs

- http://127.0.0.1:8000/docs: interactive API docs
- http://127.0.0.1:8000/health: backend and engine status
- http://127.0.0.1:8000/engine/snapshot: everything the dashboard shows, as JSON

---

## 5. Test

```powershell
uv run pytest            # full suite (~2 s)
uv run pytest -q tests/test_live_safety.py   # one file
uv run pytest -k backtest                    # by keyword
```

What's covered:

- indicators against independent pandas references
- candle building
- paper fills and charges
- OMS brackets and OCO
- risk checks
- backtest look-ahead protection and accounting
- the live-trading safety guard (with a fake Kite)
- option greeks and chain
- the options planner (CE/PE, buy/sell, strike, expiry), option backtests and the trade journal

**Add a test for any change to risk, OMS, broker or engine code.**

---

## 6. Project layout

```
app/                     core library (no web/UI code)
  core/                  domain models (Order, Signal, Candle...) and the IST clock
  market_data/           timeframes, tick→candle aggregation, instrument master, historical data, live feeds
  indicators/            incremental indicators + per-instrument engine
  patterns/              candlestick, price-action, chart patterns
  strategies/            Strategy interface + built-in strategies
  signals/               signal scoring
  risk/                  risk manager, position sizing, kill switch
  orders/                order management system (OMS)
  execution/             BrokerInterface, charges, paper/ broker, zerodha/ (session, guard, broker)
  portfolio/             positions, trades, metrics
  backtesting/           backtester (reuses the live engine)
  engine/                TradingEngine (pipeline) + EngineRuntime (threads, persistence, recovery)
  fno/                   option chain, IV/greeks, contract selection
  options/               options automation: option market (Kite / model) + planner (BUY/SELL CE/PE)
  database/              SQLite repository
  config/                settings (.env) and YAML config loading
backend/                 FastAPI app + routers (auth, account, market, engine, backtest)
frontend/                Streamlit app: streamlit_app.py + app_pages/ + charts.py (lightweight charts) + ui.py
config/trading.yaml      strategies, scoring, risk, paper fill model
tests/                   pytest suite
docs/ARCHITECTURE.md     design document
```

**Rules of thumb:**

- Strategies never import brokers.
- The UI never imports `app/`; it only calls the API.
- Only the OMS talks to a broker.
- Engine state is only touched on the engine worker thread. API handlers go through `runtime.call(...)`.

---

## 7. Options automation and the paper pilot

The app's main job is to trade index and stock **options** by itself.

### 7.1 How a trade happens

1. **Analyse.** A strategy with `execution: options` watches the underlying's **future**, for example
   `FUT:NIFTY`, which has the volume that VWAP and volume indicators need. At each candle close it may form a
   **bullish** or **bearish** view, with a stop and target *on the underlying*.
2. **Score.** The view is scored 0–100 (trend, EMA, RSI, VWAP, volume, price action, candle patterns).
   Weak signals are skipped.
3. **Plan.** The planner (`app/options/planner.py`) picks the trade:

   | View | Trending (ADX ≥ `buy_min_adx`) and options not expensive | Ranging, or options expensive (IV/realised vol ≥ `iv_rich_ratio`) |
   |---|---|---|
   | Bullish | **Buy call** (ATM, \|delta\| ≈ `buy_delta`) | **Sell put** (OTM, \|delta\| ≈ `sell_delta`) |
   | Bearish | **Buy put** | **Sell call** |

   It then picks:
   - the expiry: nearest, but at least `min_dte_buy` / `min_dte_sell` days away
   - the strike: by delta, with liquidity filters on spread and premium
   - the premium stop and target: converted from the underlying's levels via delta and gamma, then capped
4. **Risk.** The trade must pass every risk rule. Size comes from the premium stop: one stop-out costs at most
   `max_risk_per_trade`. Option buys are also capped by `max_premium_per_trade`, and option sells by
   `max_short_option_lots`.
5. **Order.** An entry is sent, followed by a stop-loss and a target. Stops are **stop-limit** orders, because
   exchanges don't accept SL-M on options. A watchdog exits at market if the price jumps past the stop's limit.
6. **Exit.** Whichever comes first:
   - the premium stop or target
   - the underlying hitting the strategy's stop or target
   - the strategy's exit signal
   - 15:15 intraday square-off
   - expiry-day exit at 15:00
   - the emergency stop
7. **Journal.** Each trade is written to the `journal` table in `data/trading.db`: entry and exit time and
   premium, call/put, buy/sell, strike, expiry, days to expiry, premium and underlying stop/target, planned
   R:R and risk, the actual R multiple, IV, delta, ADX, signal score, every reason, the exit reason, charges
   and net P&L. See it on **Trade journal**, or download it as CSV.

### 7.2 Configure

- **Global options settings:** in `config/trading.yaml` under `options:`. The `style` key is `auto`,
  `buy_only` or `sell_only`.
- **Per strategy:** add an `options:` block to a strategy entry to override any global key:

  ```yaml
  strategies:
    - id: nifty_options_trend
      type: ema_crossover
      execution: options          # trade options chosen by the planner
      instruments: ["FUT:NIFTY"]  # analysed underlying
      timeframe: 5m
      params: {fast: 9, slow: 21}
      options: {style: buy_only}  # optional per-strategy override
  ```

- **Option-selling limits:** set under `risk:`. Use `allow_option_selling`, `max_short_option_lots` (keep it
  at 1 to start), `max_premium_per_trade` and `min_option_premium`.

> Option **selling** can lose much more than the premium received. Keep it at 1 lot during the pilot, or set
> `style: buy_only`.

### 7.3 Run the paper pilot

1. Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`.
2. Every morning before 09:15, start the backend and dashboard and **log in with Kite**.
3. The engine streams live NIFTY/BANKNIFTY/SENSEX futures and the option contracts it trades. It places
   orders only in the local paper broker, which simulates bid/ask fills, slippage, partial fills and full
   charges. With Kite logged in, it also uses Kite's real margin figures.
4. Watch **Trading desk** during the day. Review **Trade journal** after the close: win rate, average R, and
   results by trade type and exit reason.
5. Use the **Auto-trading** switch to pause new trades. Open trades stay managed. **Emergency stop**
   flattens everything.

### 7.4 After the pilot: going live

Go live only after several days of stable paper results:

1. Set `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true`, and keep `LIVE_MAX_ORDER_LOTS=1`.
2. Restart. The app checks that its positions match Zerodha.
3. Arm live trading on **Risk & safety** by typing the confirmation phrase. Arming expires daily.
4. Confirm Zerodha's current API-trading requirements first, including SEBI's algo rules (static IP, algo
   tagging), and verify the charge rates.

### 7.5 Watchlists

Kite Connect has **no API for the watchlists in your Kite app**, so watchlists live in this app, in the
`watchlists` and `watchlist_items` tables. You can keep as many as you like. Fill them on the **Watchlists**
page in three ways:
- **Add instruments:** search NSE/BSE/NFO/BFO and filter by stock, index, future or option.
- **Import → My holdings / My positions:** pulls from your Zerodha account.
- **Import → Paste symbols:** for example `INFY, TCS, NIFTY 50, BANKNIFTY, NSE:SBIN`, copied from Kite.

Watched instruments stream live over the WebSocket. Before the first tick, or outside market hours, a cached
REST snapshot is used. The table can be filtered by text, type, gainers/losers and change % range, and sorted
by any column. Selecting a row opens its chart. The sidebar shows the selected list on every page; click a
row to jump to it. The **Trading desk** chart can also show any instrument from that list.

Watchlists are for monitoring. To have the bot **trade** a stock's options, add a strategy for it in
`config/trading.yaml`, with `instruments: ["FUT:RELIANCE"]` and `execution: options`.

### 7.6 Simulated session (no Kite needed)

With `FEED_SOURCE=synthetic`:
- futures move as random walks
- option chains are priced with Black-76 (IV set by `synthetic.option_iv`)
- held contracts are repriced from the underlying

Use it to test the whole flow outside market hours. Its P&L means nothing. Option **backtests** use the same
pricing model, so they test the *logic*, not real option profitability. The paper pilot on live quotes is the
real test.

---

## 8. Common tasks

### Add a strategy

1. Create a subclass of `Strategy` (see `app/strategies/library.py` for examples):

   ```python
   from app.strategies.base import Strategy
   from app.strategies.library import register

   @register
   class MyStrategy(Strategy):
       name = "my_strategy"
       description = "What it does"
       default_params = {"period": 20, "atr_period": 14}

       def indicators(self):                   # everything you read with ctx.ind() must be declared here
           return [self.spec("ema", period=self.params["period"]), self.spec("atr", period=14)]

       def on_candle(self, ctx):               # called on each CLOSED candle
           ...
           return ctx.signal("BUY", stop_loss=..., target=..., reason=["..."])  # or None
   ```

2. Make sure the module is imported. Either put the class in `library.py` or import your module there.
3. Backtest it from the **Backtest** page.
4. Add it to `config/trading.yaml` under `strategies:` to paper-trade it.

### Add an indicator

Subclass `Indicator` in `app/indicators/library.py`. Implement `update(candle)`: it must be incremental and use
closed candles only. Then add the class to `REGISTRY` and add a test comparing it to a reference calculation in
`tests/test_indicators.py`. To make it selectable on the Trading desk chart, also add it to `_POSITIONAL` in
`backend/routers/market.py` and `INDICATORS` in `frontend/app_pages/desk.py`.

### Change risk limits

Edit `risk:` in `config/trading.yaml`, or set the `MAX_*` variables in `.env`. Then restart the backend, or call
`POST /engine/restart`.

### Reset local state

Stop the backend and delete `data/` (database and recovery state) and `logs/`. To force a fresh Kite login, also
delete `.kite_session.json`.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `uv : The term 'uv' is not recognized` | uv isn't on PATH. Use `python -m uv ...` or see [Installing uv](#installing-uv). |
| `ModuleNotFoundError: No module named 'backend'` (or `'app'`) | You're not in the project root. `cd C:\Coding\trade`. |
| Dashboard says *Backend not reachable* | Start the backend first. Check `BACKEND_URL` in `.env`. |
| Engine status `waiting_for_login` | Log in with Kite from the sidebar, or use `FEED_SOURCE=synthetic`. |
| *Log in with Kite* button missing | `BROKER_API_KEY` / `BROKER_API_SECRET` are empty in `.env`. |
| Kite login redirects to an error page | The Redirect URL in the Kite app must be exactly `http://127.0.0.1:8000/auth/callback`. |
| `warmup_failed` / *Historical data unavailable* | Your Kite app lacks the historical-data add-on. Indicators warm up from live ticks instead. Backtest with CSV or synthetic data. |
| Every decision says *Blocked by risk rules* | Open **Trading desk → All decisions** for the reason. Usually one lot risks more than `max_risk_per_trade`, margin is insufficient, option selling is disabled, or it's outside the 09:20–15:00 entry window. |
| *No suitable option contract* | No expiry with enough days left, or every strike failed the liquidity filters (`max_spread_pct`, `min_premium`, `min_oi`). |
| Chart zoom resets | It redraws when its data refreshes. Turn **Live update** off on the desk to zoom and scroll freely. |
| No signals at all | Strategies need warmup candles (`warmup_bars`, default 60), and ticks only flow during market hours. |
| Port 8000/8501 already in use | Find the process with `netstat -ano \| findstr :8000`, then `taskkill /PID <pid> /F`. Or pass `--port`. |

---

## 10. Safety while developing

- Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`.
- Paper mode never sends orders to Zerodha. The red **🔴 LIVE TRADING** banner appears only in live mode.
- Never commit `.env`, `.kite_session.json`, `data/` or `logs/`. All are gitignored.
- Keep the backend bound to `127.0.0.1`. It can control trading and has no authentication of its own.
