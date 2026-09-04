"""End-to-end tests through the HTTP API."""

from __future__ import annotations

import dataclasses
import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_manifest


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_health(client, settings):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["download_dir_writable"] is True


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "PS5 Patch Downloader" in response.text


def test_settings_roundtrip(client):
    assert client.get("/api/settings").json()["max_firmware"] == ""

    response = client.put("/api/settings", json={"max_firmware": "12.60", "max_bandwidth_mbps": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["max_firmware"] == "12.60"
    assert body["max_bandwidth_mbps"] == 50

    assert client.get("/api/settings").json()["max_firmware"] == "12.60"


def test_search_without_a_reachable_index_returns_a_hint(client):
    response = client.get("/api/search", params={"q": "spider-man"})
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert "Title ID" in body["hint"]


def test_search_rejects_an_empty_query(client):
    assert client.get("/api/search", params={"q": ""}).status_code == 422


def test_unknown_title_is_a_404(client):
    response = client.get("/api/title/PPSA99999")
    assert response.status_code == 404


def test_invalid_title_id_is_rejected(client):
    assert client.get("/api/title/not-a-title").status_code == 404


def test_download_lifecycle_through_the_api(client, cdn, settings):
    manifest_url = make_manifest(cdn, [os.urandom(40_000)])

    created = client.post("/api/download", json={
        "manifest_url": manifest_url,
        "title_id": "PPSA08338",
        "content_ver": "01.004.003",
    })
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    listing = client.get("/api/downloads").json()
    assert [job["id"] for job in listing["downloads"]] == [job_id]

    assert client.get(f"/api/download/{job_id}").json()["title_id"] == "PPSA08338"

    # Pause is accepted whether the job is queued or already running.
    paused = client.post(f"/api/download/{job_id}/pause")
    assert paused.status_code == 200

    assert client.post(f"/api/download/{job_id}/resume").status_code == 200
    assert client.delete(f"/api/download/{job_id}").status_code == 204
    assert client.get("/api/downloads").json()["downloads"] == []


def test_download_rejects_system_software_urls(client):
    response = client.post("/api/download", json={"manifest_url": "https://cdn/PS5UPDATE.PUP"})
    assert response.status_code == 400
    assert "system software" in response.json()["detail"]


def test_download_rejects_ps5_package_pieces(client):
    response = client.post("/api/download", json={
        "manifest_url": "https://gst.prod.dl.playstation.net/gst/prod/00/T/app/pkg/1/f_a/C_0.pkg",
    })
    assert response.status_code == 400
    assert "_sc.pkg" in response.json()["detail"]


def test_download_requires_something_to_resolve(client):
    response = client.post("/api/download", json={})
    assert response.status_code == 400


def test_rules_are_readable_and_reloadable(client, settings):
    body = client.get("/api/rules").json()
    assert "title_page" in body["content"]
    assert body["path"] == str(settings.prospero_rules_file)
    assert client.post("/api/rules/reload").status_code == 200


def test_rules_editor_rejects_invalid_yaml(client):
    response = client.post("/api/rules", content="::: not yaml :::", headers={"Content-Type": "text/plain"})
    assert response.status_code == 400


def test_rules_editor_accepts_valid_yaml(client, settings):
    response = client.post(
        "/api/rules",
        content="version: 7\ntitle_page:\n  path: '/{title_id}'\n",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 7
    assert "version: 7" in settings.prospero_rules_file.read_text()


def test_api_token_is_enforced(settings):
    secured = dataclasses.replace(settings, api_token="s3cret")
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200  # healthcheck stays open
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings", headers={"X-API-Token": "s3cret"}).status_code == 200
        assert client.get("/api/settings", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/api/settings", headers={"X-API-Token": "wrong"}).status_code == 401


def test_basic_auth_is_enforced(settings):
    secured = dataclasses.replace(settings, auth_username="unraid", auth_password="pw")
    app = create_app(secured)
    with TestClient(app) as client:
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings", auth=("unraid", "pw")).status_code == 200
        assert client.get("/api/settings", auth=("unraid", "nope")).status_code == 401


def test_read_only_mode_blocks_writes(settings, cdn):
    read_only = dataclasses.replace(settings, read_only=True)
    app = create_app(read_only)
    with TestClient(app) as client:
        assert client.get("/api/downloads").status_code == 200
        response = client.post("/api/download", json={"manifest_url": make_manifest(cdn, [b"x"])})
        assert response.status_code == 403


def test_openapi_document_is_published(client):
    document = client.get("/api/openapi.json").json()
    for path in ("/api/search", "/api/downloads", "/api/download", "/api/title/{title_id}/updates"):
        assert path in document["paths"]
