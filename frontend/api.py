"""Thin HTTP client for the FastAPI backend. The UI never touches broker or engine objects directly."""

import os

import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


class ApiError(Exception):
    pass


def call(method: str, path: str, timeout: float = 30, **kwargs):
    try:
        r = requests.request(method, f"{BACKEND_URL}{path}", timeout=timeout, **kwargs)
    except requests.ConnectionError:
        raise ApiError(f"Backend not reachable at {BACKEND_URL}. Start it with: "
                       "uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000")
    except requests.Timeout:
        raise ApiError(f"Backend timed out on {path}")
    if not r.ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ApiError(f"{r.status_code}: {detail}")
    return r.json()


def get(path: str, **params):
    return call("GET", path, params=params)


def post(path: str, body: dict | None = None, timeout: float = 30):
    return call("POST", path, json=body or {}, timeout=timeout)


def safe_get(path: str, **params):
    """GET that renders the error in the UI and returns None instead of raising."""
    try:
        return get(path, **params)
    except ApiError as e:
        st.error(str(e))
        return None


def action(fn, success: str | None = None):
    """Run a mutating API call, show the outcome as a toast/error. Returns the result or None."""
    try:
        res = fn()
        if success:
            st.toast(success, icon=":material/check_circle:")
        return res
    except ApiError as e:
        st.error(str(e))
        return None
