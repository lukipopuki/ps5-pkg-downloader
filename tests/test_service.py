"""The resolution chain and the metadata cache."""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.service import AppService, ServiceError
from conftest import make_manifest

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def service(settings):
    instance = AppService(settings)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def publish_version_xml(cdn, fixture_dir, path="/np/PPSA08338_00/uuid-version.xml"):
    return cdn.add(path, (fixture_dir / "version_ppsa08338.xml").read_bytes())


async def test_version_xml_registration_and_resolution(service, cdn, fixture_dir):
    url = publish_version_xml(cdn, fixture_dir)

    registered = await service.register_version_xml("PPSA08338", url)
    assert len(registered["packages"]) == 2
    assert registered["packages"][0]["required_firmware"] == "10.01"

    resolved = await service.resolve_manifest("PPSA08338", "01.004.003")
    assert resolved.source == "sony-version-xml"
    assert resolved.manifest_url.endswith("MARVELSPIDERMAN2.json")

    dlc = await service.resolve_manifest(
        "PPSA08338", "01.000.000", kind="ac", content_id="EP9000-PPSA08338_00-SPIDERMAN2DLC001"
    )
    assert dlc.manifest_url.endswith("SPIDERMAN2DLC001.json")


async def test_version_xml_of_another_title_is_refused(service, cdn, fixture_dir):
    url = publish_version_xml(cdn, fixture_dir)
    with pytest.raises(ServiceError):
        await service.register_version_xml("PPSA00000", url)


async def test_resolution_without_any_source_explains_the_options(service):
    result = await service.resolve_manifest("PPSA08338", "01.001.000")
    assert result.manifest_url is None
    assert "version.xml" in result.message and "_sc.pkg" in result.message


async def test_unknown_version_is_not_resolved_from_version_xml(service, cdn, fixture_dir):
    await service.register_version_xml("PPSA08338", publish_version_xml(cdn, fixture_dir))
    # version.xml only ever describes the current patch.
    result = await service.resolve_manifest("PPSA08338", "01.000.001")
    assert result.manifest_url is None


async def test_firmware_filter_blocks_and_can_be_overridden(service, settings, cdn):
    manifest_url = make_manifest(cdn, [b"payload"])
    await service.db.store_title(
        "PPSA08338", "Marvel's Spider-Man 2", "EU", "EP9000-PPSA08338_00-MARVELSPIDERMAN2", "",
        {
            "title_id": "PPSA08338",
            "name": "Marvel's Spider-Man 2",
            "patches": [
                {"content_ver": "01.005.000", "required_firmware": "13.00",
                 "manifest_url": manifest_url, "is_latest": True},
            ],
        },
    )
    await service.update_settings({"max_firmware": "12.60"})

    with pytest.raises(ServiceError) as excinfo:
        await service.start_download(title_id="PPSA08338", content_ver="01.005.000")
    assert "13.00" in str(excinfo.value)

    job = await service.start_download(
        title_id="PPSA08338", content_ver="01.005.000", ignore_firmware=True
    )
    assert job["status"] in {"queued", "running"}


async def test_updates_are_marked_compatible_against_the_firmware_setting(service):
    await service.db.store_title(
        "PPSA08338", "Spider-Man 2", "EU", "", "",
        {
            "title_id": "PPSA08338",
            "name": "Spider-Man 2",
            "patches": [
                {"content_ver": "01.004.003", "required_firmware": "10.01"},
                {"content_ver": "01.005.000", "required_firmware": "13.00"},
                {"content_ver": "01.000.000", "required_firmware": None},
            ],
        },
    )
    await service.update_settings({"max_firmware": "12.60"})

    payload = await service.get_title("PPSA08338")
    assert payload["cached"] is True
    flags = {u["content_ver"]: u["compatible"] for u in payload["updates"]}
    assert flags == {"01.004.003": True, "01.005.000": False, "01.000.000": None}


async def test_cache_is_used_until_the_ttl_expires(service, settings):
    await service.db.store_title(
        "PPSA08338", "Cached Title", "EU", "", "",
        {"title_id": "PPSA08338", "name": "Cached Title", "patches": []},
    )
    cached = await service.get_title("PPSA08338")
    assert cached["cached"] is True
    assert cached["title"]["name"] == "Cached Title"

    # With the TTL at zero the index is consulted again. It is unreachable
    # here, so the cached copy is served together with a visible warning
    # rather than an empty page.
    await service.update_settings({"cache_ttl_hours": 0})
    stale = await service.get_title("PPSA08338", refresh=True)
    assert stale["cached"] is True
    assert stale["warning"]
    assert stale["title"]["name"] == "Cached Title"


async def test_clearing_the_cache_expires_titles(service):
    await service.db.store_title(
        "PPSA08338", "Cached", "EU", "", "", {"title_id": "PPSA08338", "patches": []}
    )
    await service.refresh_cache()
    row = await service.db.get_title("PPSA08338")
    assert row["fetched_at"] == 0


async def test_search_by_title_id_uses_the_cache(service):
    await service.db.store_title(
        "PPSA08338", "Spider-Man 2", "EU", "", "", {"title_id": "PPSA08338", "patches": []}
    )
    result = await service.search("PPSA08338")
    assert result["results"][0]["name"] == "Spider-Man 2"


async def test_search_falls_back_to_locally_known_titles(service):
    await service.db.store_title(
        "PPSA08338", "Marvel's Spider-Man 2", "EU", "", "", {"title_id": "PPSA08338", "patches": []}
    )
    result = await service.search("spider")
    assert [r["title_id"] for r in result["results"]] == ["PPSA08338"]
    assert result["results"][0]["local"] is True


async def test_reload_rules_picks_up_file_changes(service, settings):
    settings.prospero_rules_file.write_text("version: 42\n", encoding="utf-8")
    rules = service.reload_rules()
    assert rules.version == 42


async def test_default_rules_file_is_created_in_config(settings):
    instance = AppService(settings)
    await instance.start()
    try:
        assert settings.prospero_rules_file.exists()
        assert "link_resolution" in settings.prospero_rules_file.read_text()
    finally:
        await instance.stop()


async def test_read_only_service_refuses_downloads(settings):
    instance = AppService(dataclasses.replace(settings, read_only=True))
    await instance.start()
    try:
        with pytest.raises(ServiceError):
            await instance.start_download(manifest_url="https://x.playstation.net/a/b.json")
    finally:
        await instance.stop()
