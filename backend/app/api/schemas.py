"""Request/response models for the public API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DownloadCreate(BaseModel):
    """Start a download.

    Either give ``title_id`` + ``content_ver`` and let the server resolve the
    official manifest, or pass a ``manifest_url`` (a Sony ``.json`` manifest or
    the matching ``_sc.pkg`` link) directly.
    """

    title_id: str = ""
    content_ver: str = ""
    manifest_url: str = ""
    kind: str = Field(default="app", pattern="^(app|ac)$")
    content_id: str = ""
    ignore_firmware: bool = False


class VersionXmlRegister(BaseModel):
    url: str


class SettingsUpdate(BaseModel):
    max_firmware: Optional[str] = None
    cache_ttl_hours: Optional[float] = Field(default=None, ge=0)
    max_concurrent_downloads: Optional[int] = Field(default=None, ge=1, le=16)
    max_bandwidth_mbps: Optional[float] = Field(default=None, ge=0)


class HealthResponse(BaseModel):
    status: str
    version: str
    downloads_active: int
    download_dir_writable: bool


class SearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]
    cached: bool = False
    error: Optional[str] = None
    hint: Optional[str] = None
