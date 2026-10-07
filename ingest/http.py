"""The single place network GETs happen. Tests monkeypatch `get_text` / `get_json`
(or, for the retry loop itself, `httpx.get` and `time.sleep`).

Retry policy (step 6): a bounded number of attempts with exponential backoff on
HTTP 429 and 5xx and on httpx transport errors (connection failures, timeouts).
A numeric Retry-After header is honoured, capped at MAX_RETRY_WAIT. Any other
4xx is raised immediately. The DEBUG line logs URL and params only, never
headers: bearer tokens pass through here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_WAIT = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _headers(user_agent: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"User-Agent": user_agent or "cancer-news-pipeline/0.1"}
    if extra:
        h.update(extra)
    return h


def _retry_wait(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before attempt+1: Retry-After if numeric, else exponential."""
    if response is not None:
        ra = response.headers.get("Retry-After")
        if ra:
            try:
                return min(max(float(ra), 0.0), MAX_RETRY_WAIT)
            except ValueError:
                pass  # HTTP-date form: fall through to backoff
    return min(BACKOFF_BASE_SECONDS * (2**attempt), MAX_RETRY_WAIT)


def _request(
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
    max_attempts: int,
) -> httpx.Response:
    """GET with bounded retry. The only caller of httpx.get in the project."""
    attempts = max(1, int(max_attempts))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        log.debug("GET %s params=%s (attempt %d/%d)", url, params, attempt + 1, attempts)
        try:
            r = httpx.get(
                url, params=params, headers=headers, timeout=timeout, follow_redirects=True
            )
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            wait = _retry_wait(None, attempt)
            log.warning("GET %s: %s; retrying in %.1fs", url, exc.__class__.__name__, wait)
            time.sleep(wait)
            continue
        if r.status_code in RETRY_STATUSES:
            last_exc = httpx.HTTPStatusError(
                f"HTTP {r.status_code} for {url}", request=r.request, response=r
            )
            if attempt + 1 >= attempts:
                break
            wait = _retry_wait(r, attempt)
            log.warning("GET %s: HTTP %d; retrying in %.1fs", url, r.status_code, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    assert last_exc is not None
    raise last_exc


def get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    user_agent: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    r = _request(
        url,
        params=params,
        headers=_headers(user_agent, headers),
        timeout=timeout,
        max_attempts=max_attempts,
    )
    return r.text


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    user_agent: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Any:
    r = _request(
        url,
        params=params,
        headers=_headers(user_agent, headers),
        timeout=timeout,
        max_attempts=max_attempts,
    )
    return r.json()
