"""Application service: metadata cache, resolution chain, download entry points.

The resolution chain is the heart of this application.  A patch entry from the
index only becomes downloadable once it has an official Sony manifest URL, and
there are three independent ways to get one:

1. a manifest URL already cached for that patch,
2. Sony's own ``version.xml`` for the title (authoritative, always current),
3. link discovery on PROSPEROPatches (rule driven), or a URL pasted by the user.

Every step is optional; the application stays usable when any one of them is
unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings
from .db import Database
from .download.manager import DownloadManager, DownloadRequest
from .http import build_client
from .providers import sony
from .providers.prospero import (
    Patch,
    ProsperoClient,
    ProsperoError,
    SearchResult,
    TitleDetails,
    is_title_id,
)
from .providers.rules import Rules, default_rules, ensure_rules_file, load_rules
from .providers.sony_client import SonyClient
from .versions import is_firmware_compatible, normalize_content_version

log = logging.getLogger(__name__)

SETTING_MAX_FIRMWARE = "max_firmware"
SETTING_CACHE_TTL = "cache_ttl_hours"


class ServiceError(RuntimeError):
    pass


@dataclass
class ResolveResult:
    manifest_url: Optional[str] = None
    source: str = ""
    candidates: List[str] = field(default_factory=list)
    message: str = ""


class AppService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.metadata_client: httpx.AsyncClient = build_client(settings)
        self.transfer_client: httpx.AsyncClient = build_client(settings)
        # The file in /config is created and loaded in start(); until then the
        # built-in defaults apply.
        self.rules: Rules = default_rules()
        self.prospero = ProsperoClient(settings, self.metadata_client, self.rules)
        self.sony = SonyClient(settings, self.metadata_client)
        self.manager = DownloadManager(settings, self.db, self.transfer_client, self.sony)
        self._title_locks: Dict[str, asyncio.Lock] = {}

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self.settings.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings.download_dir.mkdir(parents=True, exist_ok=True)
        ensure_rules_file(self.settings.prospero_rules_file)
        self.reload_rules()
        await self.db.connect()
        if self.settings.max_firmware and not await self.db.get_setting(SETTING_MAX_FIRMWARE):
            await self.db.set_setting(SETTING_MAX_FIRMWARE, self.settings.max_firmware)
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.shutdown()
        await self.db.close()
        await asyncio.gather(
            self.metadata_client.aclose(),
            self.transfer_client.aclose(),
            return_exceptions=True,
        )

    def reload_rules(self) -> Rules:
        self.rules = load_rules(self.settings.prospero_rules_file)
        self.prospero.set_rules(self.rules)
        log.info(
            "Loaded PROSPERO rules",
            extra={"source": str(self.rules.source), "version": self.rules.version},
        )
        return self.rules

    # -- settings -----------------------------------------------------------
    async def get_max_firmware(self) -> str:
        return await self.db.get_setting(SETTING_MAX_FIRMWARE, self.settings.max_firmware)

    async def cache_ttl_seconds(self) -> float:
        stored = await self.db.get_setting(SETTING_CACHE_TTL, "")
        try:
            hours = float(stored) if stored else self.settings.cache_ttl_hours
        except ValueError:
            hours = self.settings.cache_ttl_hours
        return max(0.0, hours) * 3600.0

    async def app_settings(self) -> Dict[str, Any]:
        return {
            "max_firmware": await self.get_max_firmware(),
            "cache_ttl_hours": round(await self.cache_ttl_seconds() / 3600.0, 3),
            "max_concurrent_downloads": self.manager.max_concurrent,
            "max_bandwidth_mbps": self.manager.bandwidth_limit_mbps,
            "download_dir": str(self.settings.download_dir),
            "config_dir": str(self.settings.config_dir),
            "prospero_base_url": self.settings.prospero_base_url,
            "rules_file": str(self.settings.prospero_rules_file),
            "rules_version": self.rules.version,
            "verify_hashes": self.settings.verify_hashes,
            "read_only": self.settings.read_only,
        }

    async def update_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        if "max_firmware" in values:
            raw = str(values["max_firmware"] or "").strip()
            await self.db.set_setting(SETTING_MAX_FIRMWARE, raw)
        if "cache_ttl_hours" in values and values["cache_ttl_hours"] is not None:
            await self.db.set_setting(SETTING_CACHE_TTL, str(float(values["cache_ttl_hours"])))
        if "max_concurrent_downloads" in values and values["max_concurrent_downloads"]:
            value = int(values["max_concurrent_downloads"])
            self.manager.set_max_concurrent(value)
            await self.db.set_setting("max_concurrent_downloads", str(value))
        if "max_bandwidth_mbps" in values and values["max_bandwidth_mbps"] is not None:
            value = float(values["max_bandwidth_mbps"])
            self.manager.set_bandwidth_limit(value)
            await self.db.set_setting("max_bandwidth_mbps", str(value))
        return await self.app_settings()

    # -- search -------------------------------------------------------------
    async def search(self, query: str, refresh: bool = False) -> Dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"query": query, "results": [], "cached": False}

        if is_title_id(query):
            title_id = query.upper()
            try:
                details = await self.get_title(title_id, refresh=refresh)
                return {
                    "query": query,
                    "cached": details.get("cached", False),
                    "results": [
                        {
                            "title_id": title_id,
                            "name": details["title"]["name"],
                            "region": details["title"]["region"],
                            "icon_url": details["title"]["icon_url"],
                        }
                    ],
                }
            except ProsperoError as exc:
                raise ServiceError(str(exc)) from exc

        ttl = await self.cache_ttl_seconds()
        if not refresh and ttl > 0:
            cached = await self.db.get_search(query)
            if cached and (time.time() - cached["fetched_at"]) < ttl:
                return {"query": query, "results": json.loads(cached["payload"]), "cached": True}

        results: List[SearchResult] = []
        error: Optional[str] = None
        try:
            results = await self.prospero.search(query)
        except Exception as exc:  # network or parsing trouble
            error = str(exc)
            log.warning("PROSPERO search for %r failed: %s", query, exc)

        payload = [r.to_dict() for r in results]
        if payload:
            await self.db.store_search(query, payload)
        else:
            # Fall back to whatever we have already looked up locally.
            rows = await self.db.search_titles(query)
            payload = [
                {
                    "title_id": row["title_id"],
                    "name": row["name"],
                    "region": row["region"],
                    "icon_url": row["icon_url"],
                    "local": True,
                }
                for row in rows
            ]
        return {
            "query": query,
            "results": payload,
            "cached": False,
            "error": error,
            "hint": (
                "No results from PROSPEROPatches. Search by Title ID (e.g. PPSA08338), or "
                "adapt the search rules in prospero_rules.yaml."
            )
            if not payload
            else None,
        }

    # -- title metadata -----------------------------------------------------
    def _lock_for(self, title_id: str) -> asyncio.Lock:
        return self._title_locks.setdefault(title_id, asyncio.Lock())

    async def get_title(self, title_id: str, refresh: bool = False) -> Dict[str, Any]:
        title_id = (title_id or "").strip().upper()
        if not is_title_id(title_id):
            raise ServiceError(f"{title_id!r} is not a PS5 title ID (expected e.g. PPSA08338)")

        async with self._lock_for(title_id):
            ttl = await self.cache_ttl_seconds()
            row = await self.db.get_title(title_id)
            if not refresh and row is not None and ttl > 0 and (time.time() - row["fetched_at"]) < ttl:
                details = self._details_from_row(row)
                return await self._title_response(details, row["version_file_uri"], cached=True)

            try:
                details = await self.prospero.fetch_title(title_id)
            except ProsperoError as exc:
                if row is not None:
                    log.warning("PROSPERO lookup failed for %s, serving cache: %s", title_id, exc)
                    details = self._details_from_row(row)
                    return await self._title_response(
                        details, row["version_file_uri"], cached=True, warning=str(exc)
                    )
                raise ServiceError(str(exc)) from exc

            version_file_uri = row["version_file_uri"] if row is not None else None
            payload = details.to_dict()
            # Carry manifest URLs we already resolved over into the fresh copy.
            if row is not None:
                previous = self._details_from_row(row)
                known = {p.content_ver: p.manifest_url for p in previous.patches if p.manifest_url}
                for patch in details.patches:
                    if not patch.manifest_url and patch.content_ver in known:
                        patch.manifest_url = known[patch.content_ver]
                payload = details.to_dict()

            await self.db.store_title(
                title_id,
                details.page.name,
                details.page.region,
                details.page.content_id,
                details.page.icon_url,
                payload,
                version_file_uri,
            )
            log.info(
                "Found title",
                # Never use a reserved LogRecord attribute ("name", "module",
                # "message", ...) as an extra key: logging raises on collision.
                extra={
                    "title": title_id,
                    "title_name": details.page.name,
                    "patches": len(details.patches),
                },
            )
            return await self._title_response(details, version_file_uri, cached=False)

    @staticmethod
    def _details_from_row(row: Any) -> TitleDetails:
        """Rebuild cached details, healing gaps from the indexed columns."""
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        details = TitleDetails.from_dict(payload if isinstance(payload, dict) else {})
        details.page.title_id = details.page.title_id or row["title_id"]
        details.page.name = details.page.name or (row["name"] or "")
        details.page.region = details.page.region or (row["region"] or "")
        details.page.content_id = details.page.content_id or (row["content_id"] or "")
        details.page.icon_url = details.page.icon_url or (row["icon_url"] or "")
        return details

    async def _title_response(
        self,
        details: TitleDetails,
        version_file_uri: Optional[str],
        cached: bool,
        warning: Optional[str] = None,
    ) -> Dict[str, Any]:
        max_firmware = await self.get_max_firmware()
        updates = []
        for patch in details.patches:
            compatible = is_firmware_compatible(patch.required_firmware, max_firmware or None)
            entry = patch.to_dict()
            entry["compatible"] = compatible
            entry["downloadable"] = bool(patch.manifest_url) or bool(version_file_uri) or patch.is_latest
            updates.append(entry)

        dlc = []
        for item in details.additional_content:
            entry = item.to_dict()
            entry["compatible"] = is_firmware_compatible(item.required_firmware, max_firmware or None)
            dlc.append(entry)

        return {
            "title": {
                "title_id": details.page.title_id,
                "name": details.page.name,
                "description": details.page.description,
                "region": details.page.region,
                "content_id": details.page.content_id,
                "publisher": details.page.publisher,
                "publisher_id": details.page.publisher_id,
                "icon_url": details.page.icon_url,
                "banner_url": details.page.banner_url,
                "version_file_uri": version_file_uri,
                "last_updated": details.last_updated,
            },
            "updates": updates,
            "additional_content": dlc,
            "regions": [r.to_dict() for r in details.regions],
            "max_firmware": max_firmware,
            "cached": cached,
            "warning": warning,
        }

    # -- version.xml --------------------------------------------------------
    async def register_version_xml(self, title_id: str, url: str) -> Dict[str, Any]:
        title_id = title_id.strip().upper()
        url = sony.assert_allowed_url(url)
        document = await self.sony.fetch_version_xml(url)
        if document.title_id and document.title_id.upper() != title_id:
            raise ServiceError(
                f"this version.xml belongs to {document.title_id}, not to {title_id}"
            )
        await self.db.set_version_file_uri(title_id, url)
        return {
            "title_id": title_id,
            "version_file_uri": url,
            "packages": [
                {
                    "kind": package.kind,
                    "content_id": package.content_id,
                    "content_ver": package.content_version,
                    "required_firmware": package.required_firmware,
                    "manifest_url": package.manifest_url,
                    "digest": package.digest,
                }
                for package in document.packages
            ],
        }

    # -- resolution ---------------------------------------------------------
    async def resolve_manifest(
        self,
        title_id: str,
        content_ver: str,
        kind: str = "app",
        content_id: str = "",
    ) -> ResolveResult:
        title_id = title_id.strip().upper()
        content_ver = normalize_content_version(content_ver)

        row = await self.db.get_title(title_id)
        details: Optional[TitleDetails] = None
        if row is not None:
            details = TitleDetails.from_dict(json.loads(row["payload"]))
            for patch in details.patches:
                if patch.content_ver == content_ver and patch.manifest_url:
                    return ResolveResult(patch.manifest_url, "cache")

        # 2. Sony version.xml - official, and the only source that never needs
        #    a third party. It always describes the current patch.
        version_file_uri = row["version_file_uri"] if row is not None else None
        if version_file_uri:
            try:
                document = await self.sony.fetch_version_xml(version_file_uri)
                for package in document.packages:
                    if package.content_version != content_ver:
                        continue
                    if kind == "ac" and content_id and package.content_id != content_id:
                        continue
                    if kind != package.kind:
                        continue
                    await self._remember_manifest(title_id, content_ver, package.manifest_url)
                    return ResolveResult(package.manifest_url, "sony-version-xml")
            except Exception as exc:
                log.warning("version.xml lookup failed for %s: %s", title_id, exc)

        # 3. PROSPEROPatches link discovery.
        patch: Optional[Patch] = None
        if details:
            patch = next((p for p in details.patches if p.content_ver == content_ver), None)
        try:
            links = await self.prospero.discover_links(title_id, patch)
        except Exception as exc:
            log.warning("link discovery failed for %s: %s", title_id, exc)
            links = []

        manifests: List[str] = []
        for link in links:
            if sony.looks_like_version_xml(link):
                try:
                    await self.register_version_xml(title_id, link)
                    return await self.resolve_manifest(title_id, content_ver, kind, content_id)
                except Exception:
                    continue
            manifest_url = sony.to_manifest_url(link)
            if manifest_url and manifest_url not in manifests:
                manifests.append(manifest_url)

        if len(manifests) == 1 and (patch is None or patch.is_latest):
            await self._remember_manifest(title_id, content_ver, manifests[0])
            return ResolveResult(manifests[0], "prospero")

        if manifests:
            return ResolveResult(
                None,
                "",
                manifests,
                "Several official links were found but none could be matched to this exact "
                "version. Pick one, or paste the .json / _sc.pkg link of the update.",
            )

        return ResolveResult(
            None,
            "",
            [],
            "No official manifest URL could be resolved for this version. Register the "
            "title's version.xml (covers the current patch), or paste the .json / _sc.pkg "
            "link of the update from PROSPEROPatches.",
        )

    async def _remember_manifest(self, title_id: str, content_ver: str, manifest_url: str) -> None:
        row = await self.db.get_title(title_id)
        if row is None:
            return
        payload = json.loads(row["payload"])
        changed = False
        for patch in payload.get("patches", []):
            if patch.get("content_ver") == content_ver and not patch.get("manifest_url"):
                patch["manifest_url"] = manifest_url
                changed = True
        if changed:
            await self.db.store_title(
                title_id,
                row["name"],
                row["region"],
                row["content_id"],
                row["icon_url"],
                payload,
                row["version_file_uri"],
            )

    # -- downloads ----------------------------------------------------------
    async def start_download(
        self,
        *,
        title_id: str = "",
        content_ver: str = "",
        manifest_url: str = "",
        kind: str = "app",
        content_id: str = "",
        ignore_firmware: bool = False,
    ) -> Dict[str, Any]:
        if self.settings.read_only:
            raise ServiceError("this instance runs in read-only mode")

        title_id = (title_id or "").strip().upper()
        content_ver = normalize_content_version(content_ver)
        title_name = ""
        required_firmware = None
        package_digest = None
        expected_size = None
        source = "manual"

        row = await self.db.get_title(title_id) if title_id else None
        details = TitleDetails.from_dict(json.loads(row["payload"])) if row else None
        if details:
            title_name = details.page.name
            if not content_id:
                content_id = details.page.content_id
            patch = next((p for p in details.patches if p.content_ver == content_ver), None)
            if patch:
                required_firmware = patch.required_firmware
                expected_size = patch.file_size

        if not manifest_url:
            if not (title_id and content_ver):
                raise ServiceError("either manifest_url, or title_id together with content_ver, is required")
            resolved = await self.resolve_manifest(title_id, content_ver, kind, content_id)
            if not resolved.manifest_url:
                raise ServiceError(resolved.message or "could not resolve an official manifest URL")
            manifest_url = resolved.manifest_url
            source = resolved.source

        max_firmware = await self.get_max_firmware()
        if max_firmware and not ignore_firmware:
            compatible = is_firmware_compatible(required_firmware, max_firmware)
            if compatible is False:
                raise ServiceError(
                    f"update requires firmware {required_firmware}, which is above the configured "
                    f"maximum of {max_firmware}. Send ignore_firmware=true to download it anyway."
                )

        # If Sony's version.xml knows this package, take its digest along - it
        # lets us verify the assembled file, not just the individual pieces.
        if row is not None and row["version_file_uri"]:
            try:
                document = await self.sony.fetch_version_xml(row["version_file_uri"])
                for package in document.packages:
                    if package.manifest_url == manifest_url:
                        package_digest = package.digest
                        required_firmware = required_firmware or package.required_firmware
                        content_id = content_id or package.content_id
                        break
            except Exception:
                pass

        request = DownloadRequest(
            manifest_url=manifest_url,
            title_id=title_id,
            title_name=title_name,
            content_id=content_id,
            content_ver=content_ver or "unknown",
            kind=kind,
            source=source,
            required_firmware=required_firmware,
            package_digest=package_digest,
            expected_size=expected_size,
        )
        return await self.manager.enqueue(request)

    # -- maintenance --------------------------------------------------------
    async def refresh_cache(self, title_id: Optional[str] = None) -> Dict[str, Any]:
        if title_id:
            return await self.get_title(title_id, refresh=True)
        await self.db.clear_cache()
        log.info("Metadata cache cleared")
        return {"cleared": True}

    def rules_path(self) -> Path:
        return self.settings.prospero_rules_file
