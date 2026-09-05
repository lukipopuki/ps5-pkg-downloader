"""Persistent download manager.

The queue lives in SQLite, so a container restart picks up exactly where it
left off: unfinished jobs go back to ``queued`` and every piece continues at
the byte offset that was last persisted.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..config import Settings
from ..db import Database
from ..providers import sony
from ..providers.sony_client import SonyClient
from . import storage
from .engine import (
    Controls,
    DownloadCancelled,
    DownloadPaused,
    PackageDownloader,
    Piece,
    verify_file_digest,
)
from .ratelimit import RateLimiter, Stopwatch

log = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATES = (STATUS_QUEUED, STATUS_RUNNING)
PERSIST_INTERVAL = 2.0


@dataclass
class DownloadRequest:
    manifest_url: str
    title_id: str = ""
    title_name: str = ""
    content_id: str = ""
    content_ver: str = ""
    kind: str = "app"
    source: str = "manual"
    required_firmware: Optional[str] = None
    package_digest: Optional[str] = None
    expected_size: Optional[int] = None


@dataclass
class RuntimeState:
    controls: Controls = field(default_factory=Controls)
    task: Optional[asyncio.Task] = None
    stopwatch: Stopwatch = field(default_factory=Stopwatch)
    pieces: List[Piece] = field(default_factory=list)
    downloaded: int = 0
    last_persist: float = 0.0
    stage: str = ""


class DownloadManagerError(RuntimeError):
    pass


class DownloadManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        client: httpx.AsyncClient,
        sony_client: SonyClient,
    ):
        self.settings = settings
        self.db = db
        self.client = client
        self.sony = sony_client
        self.limiter = RateLimiter(settings.bandwidth_limit_bytes)
        self.downloader = PackageDownloader(settings, client, self.limiter)
        self.runtime: Dict[str, RuntimeState] = {}
        self._wakeup = asyncio.Event()
        self._scheduler: Optional[asyncio.Task] = None
        self._closing = False
        self._lock = asyncio.Lock()
        self._background: set = set()
        self._max_concurrent = settings.max_concurrent_downloads

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        stored_limit = await self.db.get_setting("max_bandwidth_mbps", "")
        if stored_limit:
            try:
                self.set_bandwidth_limit(float(stored_limit))
            except ValueError:
                pass
        stored_concurrency = await self.db.get_setting("max_concurrent_downloads", "")
        if stored_concurrency.isdigit():
            self._max_concurrent = max(1, int(stored_concurrency))

        # Anything that claimed to be running when we stopped is resumable.
        rows = await self.db.fetch_all(
            "SELECT id FROM downloads WHERE status = ?", (STATUS_RUNNING,)
        )
        for row in rows:
            await self._update(row["id"], status=STATUS_QUEUED)
            log.info("Requeued interrupted download", extra={"job": row["id"]})
        self._scheduler = asyncio.create_task(self._schedule_loop(), name="download-scheduler")
        self._wakeup.set()

    async def shutdown(self) -> None:
        """Stop cleanly: pause running jobs and persist their progress."""
        self._closing = True
        self._wakeup.set()
        if self._scheduler:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
        tasks = []
        for job_id, state in list(self.runtime.items()):
            if state.task and not state.task.done():
                log.info("Stopping download for shutdown", extra={"job": job_id})
                state.controls.pause(reason="shutdown")
                tasks.append(state.task)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=20)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for job_id, state in list(self.runtime.items()):
            await self._persist_progress(job_id, state, force=True)

    # -- configuration ------------------------------------------------------
    def set_bandwidth_limit(self, mbps: float) -> None:
        rate = 0.0 if mbps <= 0 else mbps * 1_000_000 / 8.0
        self.limiter.set_rate(rate)

    def set_max_concurrent(self, value: int) -> None:
        self._max_concurrent = max(1, value)
        self._wakeup.set()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def bandwidth_limit_mbps(self) -> float:
        return round(self.limiter.rate * 8 / 1_000_000, 3) if self.limiter.rate > 0 else 0.0

    # -- queue operations ---------------------------------------------------
    async def enqueue(self, request: DownloadRequest) -> Dict[str, Any]:
        manifest_url = sony.assert_allowed_url(request.manifest_url)
        normalized = sony.to_manifest_url(manifest_url)
        if normalized is None:
            if sony.is_ps5_package_piece(manifest_url):
                raise DownloadManagerError(
                    "This is a single PS5 package piece below /app/pkg/. Its JSON manifest "
                    "uses a different revision and hash and cannot be derived from it - "
                    "use the .json or _sc.pkg link of the update."
                )
            raise DownloadManagerError(f"{manifest_url} is not a Sony package manifest URL")

        existing = await self.db.fetch_one(
            "SELECT * FROM downloads WHERE manifest_url = ? AND status IN (?, ?, ?)",
            (normalized, STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED),
        )
        if existing is not None:
            raise DownloadManagerError(
                f"This package is already in the queue (status: {existing['status']})"
            )

        title_id = (request.title_id or "").upper()
        if not title_id and request.content_id:
            parts = request.content_id.split("-")
            if len(parts) > 1:
                title_id = parts[1].split("_")[0]
        if not title_id:
            title_id = "UNKNOWN"

        paths = storage.build_paths(
            self.settings.download_dir,
            title_id,
            request.content_ver or "unknown",
            kind=request.kind,
            content_id=request.content_id,
        )
        if paths.final_path.exists():
            raise DownloadManagerError(f"{paths.final_path} already exists")

        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        await self.db.execute(
            """
            INSERT INTO downloads(id, title_id, title_name, content_id, content_ver, kind,
                                  manifest_url, source, required_firmware, package_digest,
                                  total_size, downloaded, status, output_path, temp_path,
                                  created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id, title_id, request.title_name, request.content_id, request.content_ver,
                request.kind, normalized, request.source, request.required_firmware,
                request.package_digest, request.expected_size or 0, STATUS_QUEUED,
                str(paths.final_path), str(paths.temp_path), now, now,
            ),
        )
        log.info(
            "Queued download",
            extra={
                "job": job_id, "title": title_id,
                "version": request.content_ver, "source": request.source,
            },
        )
        self._wakeup.set()
        return await self.get_job(job_id)

    async def get_job(self, job_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM downloads WHERE id = ?", (job_id,))
        if row is None:
            raise DownloadManagerError(f"unknown download {job_id}")
        return self._serialise(row)

    async def list_jobs(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM downloads ORDER BY "
            "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 "
            "WHEN 'error' THEN 3 ELSE 4 END, created_at DESC"
        )
        return [self._serialise(row) for row in rows]

    async def pause(self, job_id: str) -> Dict[str, Any]:
        row = await self._row(job_id)
        if row["status"] == STATUS_RUNNING:
            state = self.runtime.get(job_id)
            if state:
                state.controls.pause()
            # Record the new state right away instead of waiting for the
            # transfer task to wind down: otherwise an immediate resume still
            # sees "running" and is rejected.
            await self._update(job_id, status=STATUS_PAUSED)
        elif row["status"] == STATUS_QUEUED:
            await self._update(job_id, status=STATUS_PAUSED)
        else:
            raise DownloadManagerError(f"download {job_id} is {row['status']} and cannot be paused")
        return await self.get_job(job_id)

    async def resume(self, job_id: str) -> Dict[str, Any]:
        row = await self._row(job_id)
        if row["status"] not in (STATUS_PAUSED, STATUS_ERROR, STATUS_CANCELLED):
            raise DownloadManagerError(f"download {job_id} is {row['status']} and cannot be resumed")
        await self._update(job_id, status=STATUS_QUEUED, error=None)
        self._wakeup.set()
        return await self.get_job(job_id)

    async def retry(self, job_id: str, *, from_scratch: bool = False) -> Dict[str, Any]:
        row = await self._row(job_id)
        if row["status"] in (STATUS_RUNNING, STATUS_QUEUED):
            raise DownloadManagerError(f"download {job_id} is already {row['status']}")
        if from_scratch:
            await self.db.execute("DELETE FROM download_pieces WHERE download_id = ?", (job_id,))
            await self._update(job_id, downloaded=0)
            paths = self._paths_of(row)
            storage.cleanup_partial(paths)
        await self._update(job_id, status=STATUS_QUEUED, error=None, retries=row["retries"] + 1)
        self._wakeup.set()
        return await self.get_job(job_id)

    async def cancel(self, job_id: str, *, delete_files: bool = True) -> None:
        row = await self._row(job_id)
        state = self.runtime.get(job_id)
        if state and state.task and not state.task.done():
            state.controls.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(state.task), timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        if delete_files:
            paths = self._paths_of(row)
            storage.cleanup_partial(paths)
            storage.prune_empty_dirs(paths.directory, self.settings.download_dir)
        await self.db.execute("DELETE FROM download_pieces WHERE download_id = ?", (job_id,))
        await self.db.execute("DELETE FROM downloads WHERE id = ?", (job_id,))
        self.runtime.pop(job_id, None)
        log.info("Removed download", extra={"job": job_id, "files_deleted": delete_files})
        self._wakeup.set()

    # -- scheduling ---------------------------------------------------------
    async def _schedule_loop(self) -> None:
        while not self._closing:
            try:
                await self._spawn_ready_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - scheduler must never die
                log.exception("scheduler iteration failed")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()

    async def _spawn_ready_jobs(self) -> None:
        async with self._lock:
            running = sum(
                1 for state in self.runtime.values() if state.task and not state.task.done()
            )
            free = self._max_concurrent - running
            if free <= 0:
                return
            rows = await self.db.fetch_all(
                "SELECT * FROM downloads WHERE status = ? ORDER BY created_at ASC LIMIT ?",
                (STATUS_QUEUED, free),
            )
            for row in rows:
                job_id = row["id"]
                if job_id in self.runtime and self.runtime[job_id].task and not self.runtime[job_id].task.done():
                    continue
                state = RuntimeState()
                self.runtime[job_id] = state
                await self._update(job_id, status=STATUS_RUNNING, started_at=time.time(), error=None)
                state.task = asyncio.create_task(self._run_job(job_id), name=f"download-{job_id}")

    # -- job execution ------------------------------------------------------
    async def _run_job(self, job_id: str) -> None:
        state = self.runtime[job_id]
        row = await self._row(job_id)
        paths = self._paths_of(row)
        try:
            pieces = await self._load_or_fetch_pieces(job_id, row)
            state.pieces = pieces
            state.downloaded = sum(p.downloaded for p in pieces)
            total = sum(p.size for p in pieces if p.size > 0)
            if total and total != row["total_size"]:
                await self._update(job_id, total_size=total)

            available = storage.free_space(paths.directory.parent)
            if available is not None and total and available < (total - state.downloaded):
                raise DownloadManagerError(
                    f"not enough free space: {(total - state.downloaded) / 1e9:.1f} GB needed, "
                    f"{available / 1e9:.1f} GB available"
                )

            storage.ensure_directory(paths.directory)
            storage.allocate(paths.temp_path, total, self.settings.preallocate)

            log.info(
                "Starting download" if state.downloaded == 0 else "Download resumed",
                extra={
                    "job": job_id,
                    "title": row["title_id"],
                    "version": row["content_ver"],
                    "at": f"{state.downloaded / 1e9:.2f} GB",
                    "pieces": len(pieces),
                },
            )

            def on_progress(index: int, written: int) -> None:
                del index, written  # the piece objects carry the authoritative value
                total_now = sum(p.downloaded for p in pieces)
                state.stopwatch.add(max(0, total_now - state.downloaded))
                state.downloaded = total_now
                now = time.monotonic()
                if now - state.last_persist >= PERSIST_INTERVAL:
                    state.last_persist = now
                    task = asyncio.create_task(self._persist_progress(job_id, state))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)

            await self.downloader.run(pieces, paths.temp_path, state.controls, on_progress)
            await self._persist_progress(job_id, state, force=True)
            await self._finalise(job_id, row, paths, state)

        except DownloadPaused:
            await self._persist_progress(job_id, state, force=True)
            # Only claim the job if nothing happened to it while the transfer
            # was winding down - a resume in that window already moved it to
            # queued, and overwriting that would silently strand the download.
            if state.controls.pause_reason == "shutdown":
                # Not a user decision: leave it queued so the next start picks
                # it back up where it stopped.
                await self._update_if_status(job_id, STATUS_RUNNING, status=STATUS_QUEUED)
                log.info(
                    "Download interrupted by shutdown, will resume on next start",
                    extra={"job": job_id, "at": f"{state.downloaded / 1e9:.2f} GB"},
                )
            else:
                await self._update_if_status(job_id, STATUS_RUNNING, status=STATUS_PAUSED)
                log.info("Download paused", extra={"job": job_id})
        except DownloadCancelled:
            await self._persist_progress(job_id, state, force=True)
            await self._update(job_id, status=STATUS_CANCELLED)
            log.info("Download cancelled", extra={"job": job_id})
        except asyncio.CancelledError:
            await self._persist_progress(job_id, state, force=True)
            await self._update(job_id, status=STATUS_QUEUED)
            raise
        except Exception as exc:
            await self._persist_progress(job_id, state, force=True)
            message = f"{type(exc).__name__}: {exc}"
            await self._update(job_id, status=STATUS_ERROR, error=message[:2000])
            log.error("Download failed", extra={"job": job_id, "error": message})
        finally:
            self._wakeup.set()

    async def _finalise(
        self,
        job_id: str,
        row: Any,
        paths: storage.OutputPaths,
        state: RuntimeState,
    ) -> None:
        digest = row["package_digest"]
        if digest and self.settings.verify_hashes:
            log.info("Verifying package digest", extra={"job": job_id})
            ok = await verify_file_digest(paths.temp_path, "sha256", digest)
            if not ok:
                raise DownloadManagerError(
                    "SHA-256 of the assembled package does not match the value from version.xml"
                )
            log.info("SHA-256 verified", extra={"job": job_id})

        storage.fsync_file(paths.temp_path)
        storage.finalize(paths.temp_path, paths.final_path)
        try:
            paths.state_path.unlink()
        except OSError:
            pass

        await self._write_metadata(row, paths, state)
        await self._update(
            job_id,
            status=STATUS_COMPLETED,
            finished_at=time.time(),
            downloaded=state.downloaded,
            error=None,
        )
        log.info(
            "Download completed",
            extra={
                "job": job_id,
                "title": row["title_id"],
                "version": row["content_ver"],
                "path": str(paths.final_path),
            },
        )

    async def _write_metadata(self, row: Any, paths: storage.OutputPaths, state: RuntimeState) -> None:
        version_meta = {
            "title_id": row["title_id"],
            "title_name": row["title_name"],
            "content_id": row["content_id"],
            "content_version": row["content_ver"],
            "kind": row["kind"],
            "required_firmware": row["required_firmware"],
            "manifest_url": row["manifest_url"],
            "package_digest": row["package_digest"],
            "size_bytes": state.downloaded,
            "file": paths.final_path.name,
            "pieces": [
                {"index": p.index, "size": p.size, "hash": p.hash_value, "hash_algo": p.hash_algo}
                for p in state.pieces
            ],
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": row["source"],
        }
        storage.write_json_atomic(paths.directory / "metadata.json", version_meta)

        title_meta_path = paths.title_directory / "metadata.json"
        title_meta = storage.read_json(title_meta_path) or {
            "title_id": row["title_id"],
            "title_name": row["title_name"],
            "content_id": row["content_id"],
            "versions": {},
        }
        title_meta["title_name"] = row["title_name"] or title_meta.get("title_name", "")
        versions = title_meta.setdefault("versions", {})
        versions[row["content_ver"]] = {
            "kind": row["kind"],
            "required_firmware": row["required_firmware"],
            "size_bytes": state.downloaded,
            "file": str(paths.final_path.relative_to(paths.title_directory)),
            "downloaded_at": version_meta["downloaded_at"],
        }
        storage.write_json_atomic(title_meta_path, title_meta)

    # -- pieces -------------------------------------------------------------
    async def _load_or_fetch_pieces(self, job_id: str, row: Any) -> List[Piece]:
        rows = await self.db.fetch_all(
            "SELECT * FROM download_pieces WHERE download_id = ? ORDER BY idx", (job_id,)
        )
        if rows:
            return [
                Piece(
                    index=r["idx"], url=r["url"], offset=r["offset"], size=r["size"],
                    hash_value=r["hash_value"], hash_algo=r["hash_algo"],
                    downloaded=r["downloaded"], status=r["status"],
                )
                for r in rows
            ]

        manifest = await self.sony.fetch_manifest(row["manifest_url"])
        pieces = [
            Piece(
                index=p.index, url=p.url, offset=p.offset, size=p.size,
                hash_value=p.hash_value, hash_algo=p.hash_algo,
            )
            for p in manifest.pieces
        ]
        await self.db.executemany(
            "INSERT INTO download_pieces(download_id, idx, url, offset, size, hash_value, hash_algo, "
            "downloaded, status) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'pending')",
            [
                (job_id, p.index, p.url, p.offset, p.size, p.hash_value, p.hash_algo)
                for p in manifest.pieces
            ],
        )
        updates: Dict[str, Any] = {"total_size": manifest.total_size}
        if manifest.package_digest and not row["package_digest"]:
            updates["package_digest"] = manifest.package_digest
        await self._update(job_id, **updates)
        log.info(
            "Resolved Sony manifest",
            extra={"job": job_id, "pieces": len(pieces), "size": manifest.total_size},
        )
        return pieces

    async def _persist_progress(self, job_id: str, state: RuntimeState, force: bool = False) -> None:
        if not state.pieces:
            return
        try:
            await self.db.executemany(
                "UPDATE download_pieces SET downloaded = ?, status = ? WHERE download_id = ? AND idx = ?",
                [(p.downloaded, p.status, job_id, p.index) for p in state.pieces],
            )
            await self.db.execute(
                "UPDATE downloads SET downloaded = ?, updated_at = ? WHERE id = ?",
                (sum(p.downloaded for p in state.pieces), time.time(), job_id),
            )
        except Exception:  # pragma: no cover - persistence must not kill a transfer
            log.exception("could not persist progress for %s", job_id)

    # -- helpers ------------------------------------------------------------
    async def _row(self, job_id: str) -> Any:
        row = await self.db.fetch_one("SELECT * FROM downloads WHERE id = ?", (job_id,))
        if row is None:
            raise DownloadManagerError(f"unknown download {job_id}")
        return row

    def _paths_of(self, row: Any) -> storage.OutputPaths:
        final_path = Path(row["output_path"])
        temp_path = Path(row["temp_path"] or (str(final_path) + ".part"))
        # <title>/<version>/file.pkg for updates,
        # <title>/dlc/<content id>/<version>/file.pkg for additional content.
        levels = 3 if row["kind"] == "ac" else 1
        title_directory = final_path.parent
        for _ in range(levels):
            title_directory = title_directory.parent
        return storage.OutputPaths(
            directory=final_path.parent,
            final_path=final_path,
            temp_path=temp_path,
            title_directory=title_directory,
        )

    async def _update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE downloads SET {assignments} WHERE id = ?",
            (*fields.values(), job_id),
        )

    async def _update_if_status(self, job_id: str, expected: str, **fields: Any) -> None:
        """Update only while the job still is in the state we last saw."""
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE downloads SET {assignments} WHERE id = ? AND status = ?",
            (*fields.values(), job_id, expected),
        )

    def _serialise(self, row: Any) -> Dict[str, Any]:
        state = self.runtime.get(row["id"])
        downloaded = row["downloaded"]
        speed = 0.0
        if state and row["status"] == STATUS_RUNNING:
            downloaded = max(downloaded, state.downloaded)
            speed = state.stopwatch.sample()
        total = row["total_size"] or 0
        remaining = max(0, total - downloaded)
        eta = int(remaining / speed) if speed > 0 and remaining > 0 else None
        return {
            "id": row["id"],
            "title_id": row["title_id"],
            "title_name": row["title_name"],
            "content_id": row["content_id"],
            "content_ver": row["content_ver"],
            "kind": row["kind"],
            "manifest_url": row["manifest_url"],
            "source": row["source"],
            "required_firmware": row["required_firmware"],
            "status": row["status"],
            "error": row["error"],
            "total_size": total,
            "downloaded": downloaded,
            "progress": round(downloaded / total * 100, 2) if total else 0.0,
            "speed_bps": round(speed, 2),
            "eta_seconds": eta,
            "output_path": row["output_path"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "retries": row["retries"],
        }
