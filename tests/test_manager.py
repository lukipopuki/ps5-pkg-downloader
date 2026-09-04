"""Integration tests for the persistent download manager."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest

from app.db import Database
from app.download.manager import DownloadManager, DownloadManagerError, DownloadRequest
from app.http import build_client
from app.providers.sony_client import SonyClient
from conftest import make_manifest

pytestmark = pytest.mark.asyncio


async def build_manager(settings):
    db = Database(settings.db_path)
    await db.connect()
    client = build_client(settings)
    manager = DownloadManager(settings, db, client, SonyClient(settings, client))
    await manager.start()
    return manager, db, client


async def teardown(manager, db, client):
    await manager.shutdown()
    await db.close()
    await client.aclose()


async def wait_for(manager, job_id, statuses, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = await manager.get_job(job_id)
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job stayed in {job['status']}, expected one of {statuses}")


async def test_download_completes_and_writes_metadata(settings, cdn):
    blobs = [os.urandom(50_000), os.urandom(30_000)]
    manifest_url = make_manifest(cdn, blobs)
    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url,
            title_id="PPSA08338",
            title_name="Marvel's Spider-Man 2",
            content_ver="01.004.003",
            content_id="EP9000-PPSA08338_00-MARVELSPIDERMAN2",
            source="test",
            required_firmware="10.01",
        ))
        job = await wait_for(manager, job["id"], {"completed", "error"})
        assert job["status"] == "completed", job["error"]

        expected = settings.download_dir / "PPSA08338" / "01.004.003" / "PPSA08338_01.004.003.pkg"
        assert expected.exists()
        assert expected.read_bytes() == b"".join(blobs)
        assert job["downloaded"] == job["total_size"] == sum(len(b) for b in blobs)

        # No temporary files are left behind after success.
        assert not list(expected.parent.glob("*.part"))
        assert not list(expected.parent.glob("*.part.json"))

        version_meta = json.loads((expected.parent / "metadata.json").read_text())
        assert version_meta["content_version"] == "01.004.003"
        assert version_meta["required_firmware"] == "10.01"
        assert len(version_meta["pieces"]) == 2

        title_meta = json.loads((expected.parent.parent / "metadata.json").read_text())
        assert "01.004.003" in title_meta["versions"]
    finally:
        await teardown(manager, db, client)


async def test_package_digest_from_version_xml_is_verified(settings, cdn):
    blobs = [os.urandom(20_000)]
    manifest_url = make_manifest(cdn, blobs)
    digest = hashlib.sha256(b"".join(blobs)).hexdigest()
    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00001", content_ver="01.000.000",
            package_digest=digest,
        ))
        job = await wait_for(manager, job["id"], {"completed", "error"})
        assert job["status"] == "completed", job["error"]
    finally:
        await teardown(manager, db, client)


async def test_wrong_package_digest_fails_the_job(settings, cdn):
    manifest_url = make_manifest(cdn, [os.urandom(20_000)])
    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00002", content_ver="01.000.000",
            package_digest="00" * 32,
        ))
        job = await wait_for(manager, job["id"], {"completed", "error"})
        assert job["status"] == "error"
        assert "SHA-256" in job["error"]
        # The unverified data is kept as .part, never published as a .pkg.
        assert not (settings.download_dir / "PPSA00002" / "01.000.000" /
                    "PPSA00002_01.000.000.pkg").exists()
    finally:
        await teardown(manager, db, client)


async def test_download_survives_a_restart(settings, cdn):
    """Stop the manager mid-transfer, start a fresh one, continue where we left off."""
    blob = os.urandom(600_000)
    manifest_url = make_manifest(cdn, [blob])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.05
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_size = 32_000

    manager, db, client = await build_manager(settings)
    job_id = None
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00003", content_ver="01.002.000",
        ))
        job_id = job["id"]
        await wait_for(manager, job_id, {"running"})
        # Let some bytes land on disk, then simulate the container stopping.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if (await manager.get_job(job_id))["downloaded"] > 0:
                break
    finally:
        await teardown(manager, db, client)

    partial = settings.download_dir / "PPSA00003" / "01.002.000" / "PPSA00003_01.002.000.pkg.part"
    assert partial.exists()

    # A new process picks the job back up.
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.0
    manager2, db2, client2 = await build_manager(settings)
    try:
        stored = await manager2.get_job(job_id)
        # Picked up automatically - no user interaction after a restart.
        assert stored["status"] in {"queued", "running", "completed"}
        assert stored["downloaded"] > 0

        job = await wait_for(manager2, job_id, {"completed", "error"})
        assert job["status"] == "completed", job["error"]

        final = settings.download_dir / "PPSA00003" / "01.002.000" / "PPSA00003_01.002.000.pkg"
        assert final.read_bytes() == blob
        # The second run only fetched the remainder.
        assert any(header and header.startswith("bytes=") and header != "bytes=0-"
                   for _, _, header in cdn.requests)
    finally:
        await teardown(manager2, db2, client2)


async def test_pause_and_resume(settings, cdn):
    blob = os.urandom(500_000)
    manifest_url = make_manifest(cdn, [blob])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.05
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_size = 32_000

    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00004", content_ver="01.000.000",
        ))
        job_id = job["id"]
        await wait_for(manager, job_id, {"running"})
        await asyncio.sleep(0.3)
        await manager.pause(job_id)
        paused = await wait_for(manager, job_id, {"paused"})
        assert paused["downloaded"] > 0

        cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.0
        await manager.resume(job_id)
        done = await wait_for(manager, job_id, {"completed", "error"})
        assert done["status"] == "completed", done["error"]
        final = settings.download_dir / "PPSA00004" / "01.000.000" / "PPSA00004_01.000.000.pkg"
        assert final.read_bytes() == blob
    finally:
        await teardown(manager, db, client)


async def test_cancel_removes_partial_files(settings, cdn):
    blob = os.urandom(400_000)
    manifest_url = make_manifest(cdn, [blob])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.05
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_size = 32_000

    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00005", content_ver="01.000.000",
        ))
        await wait_for(manager, job["id"], {"running"})
        await asyncio.sleep(0.2)
        await manager.cancel(job["id"])

        assert await manager.list_jobs() == []
        assert not (settings.download_dir / "PPSA00005" / "01.000.000").exists()
    finally:
        await teardown(manager, db, client)


async def test_failed_job_can_be_retried(settings, cdn):
    manifest_url = make_manifest(cdn, [os.urandom(10_000)])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].fail_times = 99
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].fail_with = 500

    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00006", content_ver="01.000.000",
        ))
        failed = await wait_for(manager, job["id"], {"error"})
        assert failed["error"]

        cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].fail_times = 0
        await manager.retry(job["id"])
        done = await wait_for(manager, job["id"], {"completed", "error"})
        assert done["status"] == "completed", done["error"]
        assert done["retries"] == 1
    finally:
        await teardown(manager, db, client)


async def test_duplicate_and_invalid_requests_are_rejected(settings, cdn):
    manifest_url = make_manifest(cdn, [os.urandom(200_000)])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.05

    manager, db, client = await build_manager(settings)
    try:
        await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url, title_id="PPSA00007", content_ver="01.000.000"))
        with pytest.raises(DownloadManagerError):
            await manager.enqueue(DownloadRequest(
                manifest_url=manifest_url, title_id="PPSA00007", content_ver="01.000.000"))

        with pytest.raises(DownloadManagerError):
            await manager.enqueue(DownloadRequest(
                manifest_url=cdn.url("/app/pkg/1/f_def/TEST_0.pkg"),
                title_id="PPSA00008", content_ver="01.000.000"))
    finally:
        await teardown(manager, db, client)


async def test_concurrency_limit_is_respected(settings, cdn):
    settings_dict = settings
    manager, db, client = await build_manager(settings_dict)
    manager.set_max_concurrent(1)
    try:
        for index in range(3):
            url = make_manifest(cdn, [os.urandom(150_000)], path=f"/app/info/1/f_abc/T{index}.json")
            path = f"/app/pkg/1/f_def/TEST_0.pkg"
            cdn.resources[path].chunk_delay = 0.02
            await manager.enqueue(DownloadRequest(
                manifest_url=url, title_id=f"PPSA0001{index}", content_ver="01.000.000"))
        await asyncio.sleep(0.5)
        jobs = await manager.list_jobs()
        assert sum(1 for job in jobs if job["status"] == "running") <= 1
    finally:
        await teardown(manager, db, client)


async def test_shutdown_stops_the_transfer_and_leaves_it_queued(settings, cdn):
    """SIGTERM stops an in-flight transfer and keeps it queued, not paused.

    A pause the user did not ask for must not survive as one, otherwise the
    download would sit idle forever after a container restart.
    """
    blob = os.urandom(800_000)
    manifest_url = make_manifest(cdn, [blob])
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_delay = 0.05
    cdn.resources["/app/pkg/1/f_def/TEST_0.pkg"].chunk_size = 32_000

    manager, db, client = await build_manager(settings)
    job = await manager.enqueue(DownloadRequest(
        manifest_url=manifest_url, title_id="PPSA00020", content_ver="01.000.000",
    ))
    await wait_for(manager, job["id"], {"running"})
    await asyncio.sleep(0.3)

    await manager.shutdown()

    row = await db.fetch_one("SELECT status, downloaded, total_size FROM downloads WHERE id = ?",
                             (job["id"],))
    await db.close()
    await client.aclose()

    assert row["status"] == "queued"
    assert 0 < row["downloaded"] < row["total_size"]


async def test_additional_content_lands_in_its_own_subtree(settings, cdn):
    blob = os.urandom(20_000)
    manifest_url = make_manifest(cdn, [blob])
    manager, db, client = await build_manager(settings)
    try:
        job = await manager.enqueue(DownloadRequest(
            manifest_url=manifest_url,
            title_id="PPSA08338",
            title_name="Marvel's Spider-Man 2",
            content_id="EP9000-PPSA08338_00-SPIDERMAN2DLC001",
            content_ver="01.000.000",
            kind="ac",
        ))
        job = await wait_for(manager, job["id"], {"completed", "error"})
        assert job["status"] == "completed", job["error"]

        expected = (settings.download_dir / "PPSA08338" / "dlc" /
                    "EP9000-PPSA08338_00-SPIDERMAN2DLC001" / "01.000.000" /
                    "EP9000-PPSA08338_00-SPIDERMAN2DLC001_01.000.000.pkg")
        assert expected.read_bytes() == blob
        # The title level metadata.json belongs next to the title, not inside dlc/.
        title_meta = json.loads((settings.download_dir / "PPSA08338" / "metadata.json").read_text())
        assert "01.000.000" in title_meta["versions"]
        assert not (settings.download_dir / "PPSA08338" / "dlc" / "metadata.json").exists()
    finally:
        await teardown(manager, db, client)
