"""HTTP API.

The WebUI is only one consumer of this API; everything it does is available
to other tools as plain JSON.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Query, Request, status

from .. import security
from ..download.manager import DownloadManagerError
from ..providers.sony import SonyError
from ..service import AppService, ServiceError
from ..version import __version__
from .schemas import DownloadCreate, HealthResponse, SearchResponse, SettingsUpdate, VersionXmlRegister

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _service(request: Request) -> AppService:
    return request.app.state.service


def _fail(exc: Exception, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


# -- health -----------------------------------------------------------------
@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Dict[str, Any]:
    service = _service(request)
    writable = False
    try:
        probe = service.settings.download_dir / ".write-test"
        probe.touch()
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    active = sum(
        1 for state in service.manager.runtime.values() if state.task and not state.task.done()
    )
    return {
        "status": "ok",
        "version": __version__,
        "downloads_active": active,
        "download_dir_writable": writable,
    }


# -- metadata ---------------------------------------------------------------
@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120, description="Game name or title ID"),
    refresh: bool = Query(False, description="Bypass the metadata cache"),
) -> Dict[str, Any]:
    try:
        return await _service(request).search(q, refresh=refresh)
    except ServiceError as exc:
        raise _fail(exc) from exc


@router.get("/title/{title_id}")
async def get_title(request: Request, title_id: str, refresh: bool = False) -> Dict[str, Any]:
    try:
        return await _service(request).get_title(title_id, refresh=refresh)
    except ServiceError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc


@router.get("/title/{title_id}/updates")
async def get_updates(request: Request, title_id: str, refresh: bool = False) -> Dict[str, Any]:
    try:
        payload = await _service(request).get_title(title_id, refresh=refresh)
    except ServiceError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc
    return {
        "title_id": payload["title"]["title_id"],
        "name": payload["title"]["name"],
        "max_firmware": payload["max_firmware"],
        "updates": payload["updates"],
        "additional_content": payload["additional_content"],
        "cached": payload["cached"],
    }


@router.post("/title/{title_id}/version-xml")
async def register_version_xml(
    request: Request, title_id: str, body: VersionXmlRegister
) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.register_version_xml(title_id, body.url)
    except (ServiceError, SonyError) as exc:
        raise _fail(exc) from exc


@router.get("/resolve")
async def resolve(
    request: Request,
    title_id: str = Query(...),
    content_ver: str = Query(...),
    kind: str = Query("app", pattern="^(app|ac)$"),
    content_id: str = Query(""),
) -> Dict[str, Any]:
    result = await _service(request).resolve_manifest(title_id, content_ver, kind, content_id)
    return {
        "manifest_url": result.manifest_url,
        "source": result.source,
        "candidates": result.candidates,
        "message": result.message,
    }


# -- downloads --------------------------------------------------------------
@router.post("/download", status_code=status.HTTP_201_CREATED)
async def create_download(request: Request, body: DownloadCreate) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.start_download(
            title_id=body.title_id,
            content_ver=body.content_ver,
            manifest_url=body.manifest_url,
            kind=body.kind,
            content_id=body.content_id,
            ignore_firmware=body.ignore_firmware,
        )
    except (ServiceError, DownloadManagerError, SonyError) as exc:
        raise _fail(exc) from exc


@router.get("/downloads")
async def list_downloads(request: Request) -> Dict[str, Any]:
    service = _service(request)
    return {
        "downloads": await service.manager.list_jobs(),
        "max_concurrent_downloads": service.manager.max_concurrent,
        "max_bandwidth_mbps": service.manager.bandwidth_limit_mbps,
    }


@router.get("/download/{download_id}")
async def get_download(request: Request, download_id: str) -> Dict[str, Any]:
    try:
        return await _service(request).manager.get_job(download_id)
    except DownloadManagerError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc


@router.post("/download/{download_id}/pause")
async def pause_download(request: Request, download_id: str) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.manager.pause(download_id)
    except DownloadManagerError as exc:
        raise _fail(exc) from exc


@router.post("/download/{download_id}/resume")
async def resume_download(request: Request, download_id: str) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.manager.resume(download_id)
    except DownloadManagerError as exc:
        raise _fail(exc) from exc


@router.post("/download/{download_id}/retry")
async def retry_download(
    request: Request, download_id: str, from_scratch: bool = Query(False)
) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.manager.retry(download_id, from_scratch=from_scratch)
    except DownloadManagerError as exc:
        raise _fail(exc) from exc


@router.delete("/download/{download_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_download(
    request: Request,
    download_id: str,
    delete_files: bool = Query(True, description="Also remove the partial .part file"),
) -> None:
    service = _service(request)
    security.require_write(service.settings)
    try:
        await service.manager.cancel(download_id, delete_files=delete_files)
    except DownloadManagerError as exc:
        raise _fail(exc, status.HTTP_404_NOT_FOUND) from exc


# -- settings and maintenance ----------------------------------------------
@router.get("/settings")
async def get_settings(request: Request) -> Dict[str, Any]:
    return await _service(request).app_settings()


@router.put("/settings")
async def put_settings(request: Request, body: SettingsUpdate) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.update_settings(body.model_dump(exclude_none=True))
    except (ValueError, ServiceError) as exc:
        raise _fail(exc) from exc


@router.post("/cache/refresh")
async def refresh_cache(request: Request, title_id: str = Query("")) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    try:
        return await service.refresh_cache(title_id or None)
    except ServiceError as exc:
        raise _fail(exc) from exc


@router.get("/rules")
async def get_rules(request: Request) -> Dict[str, Any]:
    service = _service(request)
    path = service.rules_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""
    return {"path": str(path), "version": service.rules.version, "content": content}


@router.post("/rules/reload")
async def reload_rules(request: Request) -> Dict[str, Any]:
    service = _service(request)
    security.require_write(service.settings)
    rules = service.reload_rules()
    return {"path": str(rules.source), "version": rules.version}


@router.post("/rules")
async def put_rules(request: Request, content: str = Body(..., media_type="text/plain")) -> Dict[str, Any]:
    """Replace the rules file and reload it (the WebUI's rules editor)."""
    service = _service(request)
    security.require_write(service.settings)
    import yaml

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _fail(ValueError(f"invalid YAML: {exc}")) from exc
    if not isinstance(parsed, dict):
        raise _fail(ValueError("rules must be a YAML mapping"))
    path = service.rules_path()
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise _fail(exc, status.HTTP_500_INTERNAL_SERVER_ERROR) from exc
    rules = service.reload_rules()
    return {"path": str(path), "version": rules.version}
