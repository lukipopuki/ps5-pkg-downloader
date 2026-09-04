"""Runtime configuration.

Everything is driven by environment variables so the container can be
configured entirely from a docker-compose file or the Unraid template.  A
``.env`` file inside the config directory is loaded as a fallback for people
who prefer files over compose environment blocks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE file (never overrides)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _env_float(name: str, default: float, minimum: Optional[float] = None) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


@dataclass(frozen=True)
class Settings:
    # --- paths -------------------------------------------------------------
    config_dir: Path
    download_dir: Path

    # --- http server -------------------------------------------------------
    host: str
    port: int

    # --- download manager --------------------------------------------------
    max_concurrent_downloads: int
    piece_concurrency: int
    max_bandwidth_mbps: float
    chunk_size: int
    verify_hashes: bool
    preallocate: bool

    # --- outbound http -----------------------------------------------------
    http_timeout: float
    http_connect_timeout: float
    http_max_retries: int
    http_backoff_base: float
    http_backoff_max: float
    user_agent: str
    proxy_url: str
    verify_tls: bool

    # --- metadata ----------------------------------------------------------
    cache_ttl_hours: float
    prospero_base_url: str
    prospero_rules_file: Path
    max_firmware: str

    # --- logging -----------------------------------------------------------
    log_level: str
    log_format: str

    # --- security ----------------------------------------------------------
    auth_username: str
    auth_password: str
    api_token: str
    read_only: bool

    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.config_dir / "ps5-patch-downloader.sqlite3"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_username and self.auth_password) or bool(self.api_token)

    @property
    def bandwidth_limit_bytes(self) -> float:
        """Global limit in bytes/s; 0 means unlimited."""
        if self.max_bandwidth_mbps <= 0:
            return 0.0
        return self.max_bandwidth_mbps * 1_000_000 / 8.0


def load_settings() -> Settings:
    config_dir = Path(_env("CONFIG_DIR", "/config"))
    # A .env inside /config is optional; compose environment wins over it.
    _load_dotenv(config_dir / ".env")

    download_dir = Path(_env("DOWNLOAD_DIR", "/downloads"))
    rules_file = _env("PROSPERO_RULES_FILE") or str(config_dir / "prospero_rules.yaml")

    return Settings(
        config_dir=config_dir,
        download_dir=download_dir,
        host=_env("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8080, minimum=1),
        max_concurrent_downloads=_env_int("MAX_CONCURRENT_DOWNLOADS", 2, minimum=1),
        piece_concurrency=_env_int("PIECE_CONCURRENCY", 2, minimum=1),
        max_bandwidth_mbps=_env_float("MAX_BANDWIDTH_MBPS", 0.0, minimum=0.0),
        chunk_size=_env_int("DOWNLOAD_CHUNK_SIZE", 1024 * 1024, minimum=16 * 1024),
        verify_hashes=_env_bool("VERIFY_HASHES", True),
        preallocate=_env_bool("PREALLOCATE_FILES", True),
        http_timeout=_env_float("HTTP_TIMEOUT_SECONDS", 60.0, minimum=1.0),
        http_connect_timeout=_env_float("HTTP_CONNECT_TIMEOUT_SECONDS", 15.0, minimum=1.0),
        http_max_retries=_env_int("HTTP_MAX_RETRIES", 6, minimum=0),
        http_backoff_base=_env_float("HTTP_BACKOFF_BASE_SECONDS", 2.0, minimum=0.1),
        http_backoff_max=_env_float("HTTP_BACKOFF_MAX_SECONDS", 60.0, minimum=1.0),
        user_agent=_env("USER_AGENT", "ps5-patch-downloader/1.0"),
        proxy_url=_env("HTTP_PROXY_URL"),
        verify_tls=_env_bool("VERIFY_TLS", True),
        cache_ttl_hours=_env_float("CACHE_TTL_HOURS", 6.0, minimum=0.0),
        prospero_base_url=_env("PROSPERO_BASE_URL", "https://prosperopatches.com"),
        prospero_rules_file=Path(rules_file),
        max_firmware=_env("MAX_FIRMWARE"),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_format=_env("LOG_FORMAT", "text").lower(),
        auth_username=_env("AUTH_USERNAME"),
        auth_password=_env("AUTH_PASSWORD"),
        api_token=_env("API_TOKEN"),
        read_only=_env_bool("READ_ONLY", False),
        extra={"frontend_dir": _env("FRONTEND_DIR")} if _env("FRONTEND_DIR") else {},
    )
