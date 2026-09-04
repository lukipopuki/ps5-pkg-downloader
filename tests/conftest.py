"""Shared fixtures.

The download tests run against a real HTTP server on localhost rather than a
mocked transport: range handling, truncated responses and resume behaviour are
exactly the parts that mocks tend to get wrong.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings  # noqa: E402


@dataclass
class Resource:
    body: bytes
    supports_ranges: bool = True
    # Called with the request count; return a status code to fail with, or
    # a positive int to truncate the response after that many bytes.
    fail_with: Optional[int] = None
    truncate_after: Optional[int] = None
    fail_times: int = 0
    # Serve the body slowly, so a test can interrupt a transfer in flight.
    chunk_delay: float = 0.0
    chunk_size: int = 64 * 1024


@dataclass
class FakeCDN:
    resources: Dict[str, Resource] = field(default_factory=dict)
    requests: list = field(default_factory=list)
    port: int = 0

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def add(self, path: str, body: bytes, **kwargs) -> str:
        self.resources[path] = Resource(body=body, **kwargs)
        return self.url(path)

    def add_json(self, path: str, payload: dict) -> str:
        return self.add(path, json.dumps(payload).encode("utf-8"))


_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


def _make_handler(cdn: FakeCDN):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            pass

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            cdn.requests.append((self.command, path, self.headers.get("Range")))
            resource = cdn.resources.get(path)
            if resource is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if resource.fail_times > 0:
                resource.fail_times -= 1
                code = resource.fail_with or 503
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            body = resource.body
            start, end = 0, len(body) - 1
            status = 200
            range_header = self.headers.get("Range")
            if range_header and resource.supports_ranges:
                match = _RANGE_RE.match(range_header)
                if match:
                    start = int(match.group(1))
                    if match.group(2):
                        end = int(match.group(2))
                    status = 206
            chunk = body[start:end + 1]

            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes" if resource.supports_ranges else "none")

            if resource.truncate_after is not None and resource.truncate_after < len(chunk):
                # Announce the full length but close early: this is what a
                # dropped connection looks like to the client.
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk[: resource.truncate_after])
                self.close_connection = True
                resource.truncate_after = None
                return

            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            if resource.chunk_delay > 0:
                for start_at in range(0, len(chunk), resource.chunk_size):
                    self.wfile.write(chunk[start_at:start_at + resource.chunk_size])
                    self.wfile.flush()
                    time.sleep(resource.chunk_delay)
            else:
                self.wfile.write(chunk)

        do_HEAD = do_GET

    return Handler


@pytest.fixture
def cdn():
    server_holder: Dict[str, ThreadingHTTPServer] = {}
    fake = FakeCDN()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(fake))
    fake.port = server.server_address[1]
    server_holder["server"] = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    download_dir = tmp_path / "downloads"
    config_dir.mkdir()
    download_dir.mkdir()
    return Settings(
        config_dir=config_dir,
        download_dir=download_dir,
        host="127.0.0.1",
        port=8080,
        max_concurrent_downloads=2,
        piece_concurrency=2,
        max_bandwidth_mbps=0.0,
        chunk_size=16 * 1024,
        verify_hashes=True,
        preallocate=True,
        http_timeout=10.0,
        http_connect_timeout=5.0,
        http_max_retries=3,
        http_backoff_base=0.05,
        http_backoff_max=0.2,
        user_agent="ps5-patch-downloader-tests",
        proxy_url="",
        verify_tls=True,
        cache_ttl_hours=6.0,
        prospero_base_url="http://127.0.0.1:1",
        prospero_rules_file=config_dir / "prospero_rules.yaml",
        max_firmware="",
        log_level="WARNING",
        log_format="text",
        auth_username="",
        auth_password="",
        api_token="",
        read_only=False,
    )


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def make_manifest(cdn: FakeCDN, pieces: list, *, path: str = "/app/info/1/f_abc/TEST.json") -> str:
    """Publish ``pieces`` (list of bytes) as a split package + manifest."""
    import hashlib

    entries = []
    offset = 0
    for index, blob in enumerate(pieces):
        piece_path = f"/app/pkg/1/f_def/TEST_{index}.pkg"
        cdn.add(piece_path, blob)
        entries.append({
            "url": cdn.url(piece_path),
            "fileOffset": offset,
            "fileSize": len(blob),
            "hashValue": hashlib.sha256(blob).hexdigest(),
        })
        offset += len(blob)
    payload = {
        "originalFileSize": offset,
        "numberOfSplitFiles": len(pieces),
        "pieces": entries,
    }
    return cdn.add_json(path, payload)
