"""Integration tests for the transfer engine against a real HTTP server."""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app.download.engine import (
    Controls,
    DownloadCancelled,
    DownloadPaused,
    PackageDownloader,
    Piece,
    verify_file_digest,
)
from app.download.ratelimit import RateLimiter, Stopwatch
from app.http import build_client
from app.providers.sony import SonyError

pytestmark = pytest.mark.asyncio


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pieces_for(cdn, blobs, **resource_kwargs):
    pieces = []
    offset = 0
    for index, blob in enumerate(blobs):
        path = f"/pkg/part_{index}.pkg"
        cdn.add(path, blob, **resource_kwargs)
        pieces.append(
            Piece(
                index=index,
                url=cdn.url(path),
                offset=offset,
                size=len(blob),
                hash_value=sha256(blob),
                hash_algo="sha256",
            )
        )
        offset += len(blob)
    return pieces


async def run_download(settings, cdn, pieces, temp_path, controls=None, limiter=None):
    controls = controls or Controls()
    async with build_client(settings) as client:
        downloader = PackageDownloader(settings, client, limiter or RateLimiter(0))
        await downloader.run(pieces, temp_path, controls, lambda *_: None)


async def test_split_package_is_merged_in_order(settings, cdn, tmp_path):
    blobs = [os.urandom(70_000), os.urandom(40_000), os.urandom(9_000)]
    pieces = pieces_for(cdn, blobs)
    target = tmp_path / "out.pkg.part"

    await run_download(settings, cdn, pieces, target)

    assert target.read_bytes() == b"".join(blobs)
    assert all(piece.status == "done" for piece in pieces)
    assert await verify_file_digest(target, "sha256", sha256(b"".join(blobs)))


async def test_resume_continues_at_the_persisted_offset(settings, cdn, tmp_path):
    blob = os.urandom(120_000)
    pieces = pieces_for(cdn, [blob])
    target = tmp_path / "out.pkg.part"

    # Simulate a restart: half the piece is on disk and recorded as such.
    target.write_bytes(blob[:50_000])
    pieces[0].downloaded = 50_000

    await run_download(settings, cdn, pieces, target)

    assert target.read_bytes() == blob
    ranges = [header for _, _, header in cdn.requests if header]
    assert "bytes=50000-" in ranges  # only the missing tail was requested


async def test_resume_recovers_when_the_partial_file_is_shorter_than_recorded(
    settings, cdn, tmp_path
):
    blob = os.urandom(80_000)
    pieces = pieces_for(cdn, [blob])
    target = tmp_path / "out.pkg.part"
    target.write_bytes(blob[:1_000])
    pieces[0].downloaded = 40_000  # progress claims more than the file holds

    await run_download(settings, cdn, pieces, target)
    assert target.read_bytes() == blob


async def test_server_ignoring_range_restarts_the_piece(settings, cdn, tmp_path):
    blob = os.urandom(60_000)
    pieces = pieces_for(cdn, [blob], supports_ranges=False)
    target = tmp_path / "out.pkg.part"
    target.write_bytes(blob[:20_000])
    pieces[0].downloaded = 20_000

    await run_download(settings, cdn, pieces, target)
    assert target.read_bytes() == blob


async def test_transient_5xx_is_retried(settings, cdn, tmp_path):
    blob = os.urandom(30_000)
    pieces = pieces_for(cdn, [blob], fail_times=2, fail_with=503)
    target = tmp_path / "out.pkg.part"

    await run_download(settings, cdn, pieces, target)
    assert target.read_bytes() == blob


async def test_dropped_connection_is_resumed(settings, cdn, tmp_path):
    blob = os.urandom(100_000)
    pieces = pieces_for(cdn, [blob], truncate_after=30_000)
    target = tmp_path / "out.pkg.part"

    await run_download(settings, cdn, pieces, target)

    assert target.read_bytes() == blob
    # The retry asked for the remainder rather than starting over.
    assert any(header and header != "bytes=0-" for _, _, header in cdn.requests)


async def test_hash_mismatch_is_reported_after_a_clean_retry(settings, cdn, tmp_path):
    blob = os.urandom(20_000)
    pieces = pieces_for(cdn, [blob])
    pieces[0].hash_value = sha256(b"something else")
    target = tmp_path / "out.pkg.part"

    with pytest.raises(Exception) as excinfo:
        await run_download(settings, cdn, pieces, target)
    assert "expected" in str(excinfo.value)
    # The piece was fetched twice: once, then one clean retry from zero.
    assert len([r for r in cdn.requests if r[1] == "/pkg/part_0.pkg"]) == 2


async def test_missing_piece_fails_loudly(settings, cdn, tmp_path):
    pieces = [Piece(index=0, url=cdn.url("/pkg/absent.pkg"), offset=0, size=10)]
    with pytest.raises(Exception):
        await run_download(settings, cdn, pieces, tmp_path / "out.pkg.part")


async def test_system_software_urls_are_refused_by_the_engine(settings, cdn, tmp_path):
    pieces = [Piece(index=0, url=cdn.url("/PS5UPDATE.PUP"), offset=0, size=10)]
    with pytest.raises(SonyError):
        await run_download(settings, cdn, pieces, tmp_path / "out.pkg.part")


async def test_pause_keeps_partial_data(settings, cdn, tmp_path):
    blob = os.urandom(400_000)
    pieces = pieces_for(cdn, [blob], chunk_delay=0.05, chunk_size=32_000)
    target = tmp_path / "out.pkg.part"
    controls = Controls()

    async def pause_soon():
        await asyncio.sleep(0.25)
        controls.pause()

    with pytest.raises(DownloadPaused):
        await asyncio.gather(run_download(settings, cdn, pieces, target, controls), pause_soon())

    assert 0 < pieces[0].downloaded < len(blob)
    assert target.exists()

    # Resuming the same piece objects completes the file correctly.
    await run_download(settings, cdn, pieces, target, Controls())
    assert target.read_bytes() == blob


async def test_cancel_stops_the_transfer(settings, cdn, tmp_path):
    blob = os.urandom(400_000)
    pieces = pieces_for(cdn, [blob], chunk_delay=0.05, chunk_size=32_000)
    controls = Controls()

    async def cancel_soon():
        await asyncio.sleep(0.2)
        controls.cancel()

    with pytest.raises(DownloadCancelled):
        await asyncio.gather(
            run_download(settings, cdn, pieces, tmp_path / "out.pkg.part", controls), cancel_soon()
        )


async def test_bandwidth_limit_slows_the_transfer(settings, cdn, tmp_path):
    blob = os.urandom(200_000)
    pieces = pieces_for(cdn, [blob])
    limiter = RateLimiter(100_000)  # 100 kB/s

    started = asyncio.get_running_loop().time()
    await run_download(settings, cdn, pieces, tmp_path / "out.pkg.part", limiter=limiter)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed > 0.7  # 200 kB at 100 kB/s cannot finish instantly


async def test_stopwatch_reports_speed_and_eta():
    watch = Stopwatch(half_life=1.0)
    watch.add(1_000_000)
    await asyncio.sleep(0.3)
    speed = watch.sample()
    assert speed > 0
    assert watch.eta(speed * 10) is not None
    assert watch.eta(0) is None


async def test_bandwidth_limit_applies_when_a_chunk_exceeds_the_bucket(settings, cdn, tmp_path):
    """A 1 MiB read chunk must not slip past a limit below 1 MiB/s."""
    import dataclasses

    big_chunks = dataclasses.replace(settings, chunk_size=1024 * 1024)
    blob = os.urandom(1_500_000)
    pieces = pieces_for(cdn, [blob])
    limiter = RateLimiter(500_000)  # 500 kB/s, well below one chunk

    started = asyncio.get_running_loop().time()
    await run_download(big_chunks, cdn, pieces, tmp_path / "out.pkg.part", limiter=limiter)
    elapsed = asyncio.get_running_loop().time() - started

    assert (tmp_path / "out.pkg.part").read_bytes() == blob
    assert elapsed > 1.5  # 1.5 MB at 500 kB/s
