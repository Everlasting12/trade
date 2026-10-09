"""FastAPI backend: owns the Kite session and the trading engine; Streamlit is a pure HTTP client.

Run (bind to localhost only: the API can control trading and has no auth of its own):
    uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from kiteconnect.exceptions import KiteException

from backend.deps import container
from backend.routers import account, auth, backtest, engine, market, watchlists

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    c = container()
    # Engine start can take a few seconds (instrument dump, warmup); don't block the API from serving.
    threading.Thread(target=c.runtime.start, name="engine-start", daemon=True).start()
    yield
    c.runtime.stop()
    c.repo.close()


app = FastAPI(title="Algo Trading Platform API", lifespan=lifespan)


@app.exception_handler(KiteException)
def kite_error(request: Request, exc: KiteException):
    return JSONResponse(status_code=exc.code or 500, content={"detail": str(exc), "error_type": type(exc).__name__})


@app.exception_handler(TimeoutError)
def timeout_error(request: Request, exc: TimeoutError):
    return JSONResponse(status_code=504, content={"detail": "engine did not respond in time"})


@app.get("/health")
def health():
    c = container()
    return {"ok": True, "engine": c.runtime.status, "mode": str(c.settings.trading_mode)}


@app.get("/config")
def config():
    return container().settings.public()


for r in (auth.router, account.router, market.router, engine.router, backtest.router, watchlists.router):
    app.include_router(r)
