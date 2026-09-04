"""Optional HTTP Basic auth / API token.

Both are off unless the corresponding environment variables are set, and no
credential is ever written to a log line.  ``/api/health`` stays open so a
Docker healthcheck does not need credentials.
"""

from __future__ import annotations

import base64
import hmac
import logging
from fastapi import HTTPException, Request, status

from .config import Settings

log = logging.getLogger(__name__)

OPEN_PATHS = {"/api/health"}


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _check_basic(header: str, settings: Settings) -> bool:
    if not settings.auth_username or not settings.auth_password:
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except (IndexError, ValueError, UnicodeDecodeError):
        return False
    username, _, password = decoded.partition(":")
    return _constant_time_equals(username, settings.auth_username) and _constant_time_equals(
        password, settings.auth_password
    )


def _check_bearer(header: str, settings: Settings) -> bool:
    if not settings.api_token:
        return False
    try:
        token = header.split(" ", 1)[1].strip()
    except IndexError:
        return False
    return _constant_time_equals(token, settings.api_token)


def authorize(request: Request, settings: Settings) -> None:
    if not settings.auth_enabled:
        return
    if request.url.path in OPEN_PATHS or request.method == "OPTIONS":
        return

    token_header = request.headers.get("X-API-Token", "")
    if settings.api_token and token_header and _constant_time_equals(token_header, settings.api_token):
        return

    header = request.headers.get("Authorization", "")
    if header.lower().startswith("basic ") and _check_basic(header, settings):
        return
    if header.lower().startswith("bearer ") and _check_bearer(header, settings):
        return

    headers = {"WWW-Authenticate": "Basic realm=\"ps5-patch-downloader\""} if settings.auth_username else {}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required", headers=headers)


def require_write(settings: Settings) -> None:
    if settings.read_only:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this instance runs in read-only mode",
        )
