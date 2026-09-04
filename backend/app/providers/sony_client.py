"""HTTP access to Sony's official endpoints (version.xml + JSON manifests)."""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from ..config import Settings
from ..http import raise_for_retryable, with_retries
from . import sony
from .sony import PackageManifest, SonyError, VersionDocument

log = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class SonyClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def _get_text(self, url: str, description: str) -> str:
        sony.assert_allowed_url(url)

        async def attempt(_: int) -> str:
            response = await self.client.get(url)
            raise_for_retryable(response, url)
            if response.status_code >= 400:
                raise SonyError(f"HTTP {response.status_code} for {url}")
            content = response.content
            if len(content) > MAX_DOCUMENT_BYTES:
                raise SonyError(f"{description} is unexpectedly large ({len(content)} bytes)")
            return content.decode("utf-8", errors="replace")

        return await with_retries(attempt, settings=self.settings, description=description)

    async def fetch_version_xml(self, url: str) -> VersionDocument:
        if not sony.looks_like_version_xml(url):
            log.debug("fetching %s as a version.xml although the name does not match", url)
        text = await self._get_text(url, "version.xml")
        document = sony.parse_version_xml(text)
        log.info(
            "Resolved Sony version.xml", extra={"title": document.title_id, "packages": len(document.packages)}
        )
        return document

    async def fetch_manifest(self, url: str) -> PackageManifest:
        manifest_url = sony.to_manifest_url(url)
        if manifest_url is None:
            if sony.is_ps5_package_piece(url):
                raise SonyError(
                    "This is a PS5 package piece below /app/pkg/. Its JSON manifest uses a "
                    "different revision and hash, so it cannot be derived from this URL. "
                    "Use the .json or _sc.pkg link instead."
                )
            raise SonyError(f"cannot derive a JSON manifest from {url}")

        text = await self._get_text(manifest_url, "package manifest")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SonyError(f"manifest at {manifest_url} is not valid JSON: {exc}") from exc
        manifest = sony.parse_manifest(payload, manifest_url)
        log.info(
            "Resolved Sony manifest",
            extra={
                "pieces": len(manifest.pieces),
                "size": manifest.total_size,
                "split": manifest.is_split,
            },
        )
        return manifest

    async def probe(self, url: str) -> tuple[int, bool]:
        """Return ``(size, supports_ranges)`` for a package piece.

        Some Sony endpoints reject HEAD, so a one byte range request is the
        reliable probe: a 206 with a Content-Range proves both the size and
        range support in a single round trip.
        """
        sony.assert_allowed_url(url)

        async def attempt(_: int) -> tuple[int, bool]:
            response = await self.client.get(url, headers={"Range": "bytes=0-0"})
            raise_for_retryable(response, url)
            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                total = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
                if total.isdigit():
                    return int(total), True
                return -1, True
            if response.status_code == 200:
                length = response.headers.get("Content-Length")
                return (int(length) if length and length.isdigit() else -1), False
            raise SonyError(f"HTTP {response.status_code} for {url}")

        return await with_retries(attempt, settings=self.settings, description="package probe")
