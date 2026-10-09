from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from kiteconnect.exceptions import KiteException

from backend.deps import container

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/status")
def status():
    c = container()
    s = c.session
    return {"configured": s.configured, "logged_in": s.logged_in, "profile": s.profile, "login_url": s.login_url()}


@router.get("/callback")
def callback(request_token: str | None = None, status: str | None = None):
    """Registered Kite redirect URL: exchanges the request_token for the day's access_token."""
    c = container()
    front = c.settings.frontend_url
    if status != "success" or not request_token:
        return HTMLResponse(f"<h3>Kite login failed (status={status}).</h3><a href='{front}'>Back</a>", 400)
    try:
        c.session.complete_login(request_token)
    except (KiteException, RuntimeError) as e:
        return HTMLResponse(f"<h3>Session exchange failed: {e}</h3><a href='{front}'>Back</a>", 400)
    return RedirectResponse(front)


@router.post("/logout")
def logout():
    container().session.logout()
    return {"ok": True}
