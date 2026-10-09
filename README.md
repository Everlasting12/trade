# Options Autopilot: algo trading on Zerodha Kite Connect

Automated **index and stock options** trading. Strategies analyse NIFTY, BANKNIFTY, SENSEX or stock futures with
indicators, price action and patterns. A planner then decides **buy call / buy put / sell call / sell put**,
picks the expiry and strike, and manages the trade with stops, targets and exits. Every trade is recorded in a
journal: entry and exit, R:R, reasons and P&L. Run it as a **paper-trading pilot** on live market data first;
enable live Zerodha trading only after that.

One engine runs strategies in three modes:

- **backtest**: on historical, CSV or synthetic data
- **paper trading**: live data with simulated fills; nothing is sent to Zerodha
- **live trading**: real orders through Zerodha, behind six independent safety layers

Only the execution adapter changes between modes.

- **Backend:** FastAPI ([backend/](backend/)). It owns the Kite session, the trading engine ([app/](app/)), and SQLite persistence.
- **Dashboard:** Streamlit ([frontend/](frontend/)) with Kite-style TradingView lightweight charts. Pages:
  - **Trading desk:** chart with trade markers, open positions, and the bot's decisions
  - **Option chain**
  - **Trade journal**
  - **Strategies**
  - **Backtest**
  - **Risk & safety:** emergency stop, auto-trading, live arming
  - **Activity log**
  - **Zerodha account**
- **Developer guide:** see [dev.md](dev.md) for setup, running, configuring options, and the paper-pilot workflow.
- **Design:** see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the architecture, data flow, schema, safety design, concurrency model, and roadmap.

> No strategy here is assumed profitable. Backtest on real data and paper-trade before any live use.

## Setup

1. Create a Kite Connect app at <https://developers.kite.trade/apps>. Set its **Redirect URL** to
   `http://127.0.0.1:8000/auth/callback`.
2. Copy `.env.example` to `.env`, then set `BROKER_API_KEY` and `BROKER_API_SECRET`. The older names
   `KITE_API_KEY` and `KITE_API_SECRET` also work.
3. Install dependencies: `uv sync`. If `uv` isn't on your PATH, use `python -m uv sync`.
4. Review [config/trading.yaml](config/trading.yaml), which holds strategies, scoring, risk limits, and the paper fill model.

## Run

Start the backend and dashboard in two terminals:

```
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
uv run streamlit run frontend/streamlit_app.py
```

Open <http://localhost:8501> and click **Log in with Kite**. The engine starts in **paper** mode once you're logged in.
It loads the instrument master, warms up indicators from historical data, and subscribes to the instruments
your strategies use.

**No Kite account, or outside market hours?** Run a simulated session (paper only):

```
FEED_SOURCE=synthetic SYNTHETIC_SPEED=20 uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

On Windows PowerShell, set the variables first: `$env:FEED_SOURCE="synthetic"; $env:SYNTHETIC_SPEED="20"`.

## Tests

```
uv run pytest
```

The 74 tests cover the following:

- indicators against independent pandas reference implementations
- candle aggregation (dedupe, late ticks, higher-timeframe resampling)
- paper fills (slippage, partial fills, gaps, stop-before-target)
- charges
- OMS brackets, OCO, duplicate prevention, and unknown-state reconciliation
- risk checks and sizing
- backtest look-ahead protection and accounting identities
- live-trading guard layers
- option greeks and chain
- the options planner, option backtests, and the trade journal

## Live trading

Live trading is off by default. To send real orders, **all** of these must hold:

1. `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true` are set in `.env`, followed by a restart.
2. Broker reconciliation at startup is clean, or you have explicitly acknowledged the differences.
3. You have armed live trading on **Risk & safety** by typing the confirmation phrase. Arming expires daily.
4. The kill switch is off.
5. Each order is within `LIVE_MAX_ORDER_LOTS` (default 1).

Before going live, confirm Zerodha's current API-trading requirements, including SEBI's retail algo framework
(static IP, algo tagging, order-rate limits). Also verify the charge rates in `app/execution/charges.py`.

## Security

- `.env`, `.kite_session.json`, `data/` and `logs/` are gitignored. Never commit secrets.
- In production, inject secrets from a secret manager.
- The backend has no authentication. It can place orders, so bind it to `127.0.0.1`, or put it behind an
  authenticated reverse proxy.
- The API never asks for your Zerodha password. Login uses Kite's OAuth-style redirect flow, and the access token
  expires daily.
