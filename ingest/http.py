"""The single place network GETs happen. Tests monkeypatch `get_text` / `get_json`."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


def _headers(user_agent: str | None) -> dict[str, str]:
    return {"User-Agent": user_agent or "cancer-news-pipeline/0.1"}


def get_text(url: str, *, params: dict[str, Any] | None = None, user_agent: str | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> str:
    log.debug("GET %s params=%s", url, params)
    r = httpx.get(url, params=params, headers=_headers(user_agent), timeout=timeout,
                  follow_redirects=True)
    r.raise_for_status()
    return r.text


def get_json(url: str, *, params: dict[str, Any] | None = None, user_agent: str | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> Any:
    log.debug("GET %s params=%s", url, params)
    r = httpx.get(url, params=params, headers=_headers(user_agent), timeout=timeout,
                  follow_redirects=True)
    r.raise_for_status()
    return r.json()
