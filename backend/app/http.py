"""Shared outbound HTTP helpers: one client, one retry policy."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

from .config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RetryableHTTPError(Exception):
    """A transport level or 5xx failure that is worth retrying."""


def build_client(settings: Settings, *, follow_redirects: bool = True) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        settings.http_timeout,
        connect=settings.http_connect_timeout,
        read=settings.http_timeout,
        write=settings.http_timeout,
        pool=settings.http_connect_timeout,
    )
    limits = httpx.Limits(max_connections=32, max_keepalive_connections=16)
    kwargs = {
        "timeout": timeout,
        "limits": limits,
        "follow_redirects": follow_redirects,
        "headers": {
            "User-Agent": settings.user_agent,
            "Accept-Encoding": "identity",
        },
        "verify": settings.verify_tls,
    }
    if settings.proxy_url:
        kwargs["proxy"] = settings.proxy_url
    return httpx.AsyncClient(**kwargs)


def backoff_delay(attempt: int, base: float, maximum: float) -> float:
    """Exponential backoff with full jitter."""
    raw = min(maximum, base * (2 ** max(0, attempt - 1)))
    return random.uniform(raw / 2.0, raw)


async def with_retries(
    operation: Callable[[int], Awaitable[T]],
    *,
    settings: Settings,
    description: str,
    max_retries: Optional[int] = None,
    on_retry: Optional[Callable[[int, float, Exception], None]] = None,
) -> T:
    """Run ``operation(attempt)`` until it succeeds or retries are exhausted."""
    attempts = settings.http_max_retries if max_retries is None else max_retries
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 2):
        try:
            return await operation(attempt)
        except asyncio.CancelledError:
            raise
        except (RetryableHTTPError, httpx.TransportError, httpx.RemoteProtocolError) as exc:
            last_error = exc
            if attempt > attempts:
                break
            delay = backoff_delay(attempt, settings.http_backoff_base, settings.http_backoff_max)
            if on_retry:
                on_retry(attempt, delay, exc)
            else:
                log.warning(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    description, attempt, attempts + 1, delay, exc,
                )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def raise_for_retryable(response: httpx.Response, url: str) -> None:
    """Turn retryable status codes into RetryableHTTPError, others into HTTPStatusError."""
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableHTTPError(f"HTTP {response.status_code} for {url}")
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"HTTP {response.status_code} for {url}", request=response.request, response=response
        )
