"""The actual transfer engine.

One package consists of one or more manifest pieces.  Every piece is a
separate URL that is written into the final file at its own ``fileOffset``,
which is exactly how ``fetchpkg`` merges split packages - only here the write
happens through ``pwrite`` at an absolute offset, so pieces can run in
parallel and each one can be resumed on its own.

Resume works without any extra bookkeeping: the number of bytes a piece has
already written is persisted, and a restart continues with
``Range: bytes=<written>-``.  To keep the SHA-256/SHA-1 verification honest
across restarts, the already written prefix is streamed back through the hash
function before the transfer continues.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from ..config import Settings
from ..http import RetryableHTTPError, backoff_delay
from ..providers import sony
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

READBACK_CHUNK = 4 * 1024 * 1024


class DownloadCancelled(Exception):
    """The job was cancelled by the user."""


class DownloadPaused(Exception):
    """The job was paused; partial data and state are kept."""


class HashMismatch(Exception):
    def __init__(self, index: int, expected: str, actual: str):
        super().__init__(f"piece {index}: expected {expected}, got {actual}")
        self.index = index
        self.expected = expected
        self.actual = actual


@dataclass
class Piece:
    index: int
    url: str
    offset: int
    size: int
    hash_value: Optional[str] = None
    hash_algo: Optional[str] = None
    downloaded: int = 0
    status: str = "pending"

    @property
    def remaining(self) -> int:
        if self.size < 0:
            return 0
        return max(0, self.size - self.downloaded)

    @property
    def is_complete(self) -> bool:
        return self.size >= 0 and self.downloaded >= self.size and self.status == "done"


class Controls:
    """Pause / cancel signalling for one job.

    ``pause_reason`` separates a pause the user asked for from one caused by
    the process shutting down: the first must stay paused, the second has to
    come back as queued so the download continues after a restart.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._paused = False
        self.pause_reason = ""

    def cancel(self) -> None:
        self._cancelled = True

    def pause(self, reason: str = "user") -> None:
        self._paused = True
        self.pause_reason = reason or "user"

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return self._paused

    def check(self) -> None:
        if self._cancelled:
            raise DownloadCancelled()
        if self._paused:
            raise DownloadPaused()


ProgressCallback = Callable[[int, int], None]  # (piece_index, bytes_written)


class PackageDownloader:
    """Downloads all pieces of one package into a single output file."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        limiter: RateLimiter,
    ):
        self.settings = settings
        self.client = client
        self.limiter = limiter

    async def run(
        self,
        pieces: List[Piece],
        temp_path: Path,
        controls: Controls,
        on_progress: ProgressCallback,
        *,
        piece_concurrency: Optional[int] = None,
    ) -> None:
        concurrency = max(1, piece_concurrency or self.settings.piece_concurrency)
        semaphore = asyncio.Semaphore(concurrency)
        fd = os.open(temp_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            async def guarded(piece: Piece) -> None:
                async with semaphore:
                    controls.check()
                    await self._download_piece(fd, piece, controls, on_progress)

            tasks = [asyncio.create_task(guarded(p)) for p in pieces if not p.is_complete]
            if not tasks:
                return
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            try:
                os.fsync(fd)
            except OSError:
                pass
            os.close(fd)

    # -- one piece ----------------------------------------------------------
    async def _download_piece(
        self,
        fd: int,
        piece: Piece,
        controls: Controls,
        on_progress: ProgressCallback,
    ) -> None:
        sony.assert_allowed_url(piece.url)
        attempts = self.settings.http_max_retries
        attempt = 0
        while True:
            attempt += 1
            controls.check()
            try:
                await self._transfer(fd, piece, controls, on_progress)
                return
            except (DownloadCancelled, DownloadPaused, asyncio.CancelledError):
                raise
            except HashMismatch as exc:
                # A corrupt piece is worth exactly one clean retry from zero.
                if attempt >= 2:
                    raise
                log.warning(
                    "Hash mismatch, re-downloading piece",
                    extra={"piece": exc.index, "expected": exc.expected, "actual": exc.actual},
                )
                piece.downloaded = 0
                piece.status = "pending"
                on_progress(piece.index, 0)
            except (RetryableHTTPError, httpx.TransportError, httpx.RemoteProtocolError, OSError) as exc:
                if attempt > attempts:
                    raise
                delay = backoff_delay(attempt, self.settings.http_backoff_base, self.settings.http_backoff_max)
                log.warning(
                    "Piece %d failed (attempt %d/%d), retrying in %.1fs: %s",
                    piece.index, attempt, attempts + 1, delay, exc,
                )
                await self._sleep_interruptible(delay, controls)

    async def _sleep_interruptible(self, delay: float, controls: Controls) -> None:
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            controls.check()
            await asyncio.sleep(min(0.5, deadline - time.monotonic()))

    async def _transfer(
        self,
        fd: int,
        piece: Piece,
        controls: Controls,
        on_progress: ProgressCallback,
    ) -> None:
        hasher = None
        if self.settings.verify_hashes and piece.hash_value and piece.hash_algo:
            hasher = hashlib.new(piece.hash_algo)
            if piece.downloaded > 0:
                await self._rehash_prefix(fd, piece, hasher, controls)

        headers = {"Accept-Encoding": "identity"}
        if piece.downloaded > 0:
            headers["Range"] = f"bytes={piece.downloaded}-"

        start_offset = piece.downloaded
        async with self.client.stream("GET", piece.url, headers=headers) as response:
            if response.status_code in (408, 425, 429, 500, 502, 503, 504):
                raise RetryableHTTPError(f"HTTP {response.status_code} for {piece.url}")
            if piece.downloaded > 0 and response.status_code == 200:
                # The server ignored our Range header: start over rather than
                # writing the whole file at the resume offset.
                log.warning("Server ignored Range for piece %d, restarting it", piece.index)
                piece.downloaded = 0
                start_offset = 0
                on_progress(piece.index, 0)
                if hasher is not None:
                    hasher = hashlib.new(piece.hash_algo)  # type: ignore[arg-type]
            elif piece.downloaded > 0 and response.status_code != 206:
                raise RetryableHTTPError(
                    f"HTTP {response.status_code} while resuming piece {piece.index}"
                )
            elif piece.downloaded == 0 and response.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code} for {piece.url}",
                    request=response.request,
                    response=response,
                )

            if piece.size < 0:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit():
                    piece.size = start_offset + int(declared)

            position = start_offset
            async for chunk in response.aiter_bytes(self.settings.chunk_size):
                if not chunk:
                    continue
                controls.check()
                await self.limiter.acquire(len(chunk))
                await asyncio.to_thread(os.pwrite, fd, chunk, piece.offset + position)
                if hasher is not None:
                    hasher.update(chunk)
                position += len(chunk)
                piece.downloaded = position
                on_progress(piece.index, position)

        if piece.size >= 0 and piece.downloaded != piece.size:
            raise RetryableHTTPError(
                f"piece {piece.index} ended at {piece.downloaded} of {piece.size} bytes"
            )

        if hasher is not None and piece.hash_value:
            actual = hasher.hexdigest()
            if actual.lower() != piece.hash_value.lower():
                raise HashMismatch(piece.index, piece.hash_value, actual)
            log.info(
                "%s verified", piece.hash_algo.upper() if piece.hash_algo else "hash",
                extra={"piece": piece.index},
            )
        piece.status = "done"

    async def _rehash_prefix(self, fd: int, piece: Piece, hasher, controls: Controls) -> None:
        """Feed the already downloaded prefix back through the hash function."""
        log.info(
            "Rebuilding hash state for resumed piece",
            extra={"piece": piece.index, "bytes": piece.downloaded},
        )
        remaining = piece.downloaded
        position = 0
        while remaining > 0:
            controls.check()
            size = min(READBACK_CHUNK, remaining)
            data = await asyncio.to_thread(os.pread, fd, size, piece.offset + position)
            if not data:
                # The partial file is shorter than the recorded progress.
                piece.downloaded = position
                return
            hasher.update(data)
            position += len(data)
            remaining -= len(data)
        piece.downloaded = position


async def verify_file_digest(path: Path, algo: str, expected: str, chunk: int = READBACK_CHUNK) -> bool:
    """Verify the digest of a finished package file."""
    hasher = hashlib.new(algo)

    def _read() -> str:
        with open(path, "rb") as handle:
            while True:
                data = handle.read(chunk)
                if not data:
                    break
                hasher.update(data)
        return hasher.hexdigest()

    actual = await asyncio.to_thread(_read)
    return actual.lower() == expected.lower()
