"""Read-only Zerodha account views. Order placement is intentionally NOT exposed here: every order
goes through the engine (risk manager + OMS + live guard)."""

from fastapi import APIRouter, Depends

from backend.deps import kite

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/profile")
def profile(k=Depends(kite)):
    return k.profile()


@router.get("/margins")
def margins(k=Depends(kite)):
    return k.margins()


@router.get("/holdings")
def holdings(k=Depends(kite)):
    return k.holdings()


@router.get("/positions")
def positions(k=Depends(kite)):
    return k.positions()


@router.get("/orders")
def orders(k=Depends(kite)):
    return k.orders()


@router.get("/trades")
def trades(k=Depends(kite)):
    return k.trades()
