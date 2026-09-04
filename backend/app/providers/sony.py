"""Sony CDN access: ``version.xml`` documents and JSON package manifests.

Nothing in here is hardcoded to a specific host.  URLs always come from data
(a ``version.xml``, a manifest, or a URL the user supplied); the only
host-specific knowledge is a *deny* list that refuses system software.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from ..versions import normalize_content_version, system_ver_to_firmware

log = logging.getLogger(__name__)

# Anything that smells like PlayStation system software is refused outright.
# This tool downloads game update packages only.
FORBIDDEN_URL_PATTERNS = (
    re.compile(r"ps[45]?update.*\.pup", re.I),
    re.compile(r"\.pup(\?|$)", re.I),
    re.compile(r"/ps5/updater", re.I),
    re.compile(r"/ps4/updater", re.I),
    re.compile(r"sys(tem)?[-_]?(update|software)", re.I),
)

PLAYSTATION_LINK_RE = re.compile(
    r"https?://[A-Za-z0-9.\-]*playstation\.net/[^\s\"'<>\\)]+", re.I
)

SUPPORTED_HASHES = {40: "sha1", 64: "sha256"}


class SonyError(RuntimeError):
    """Raised for malformed Sony documents or refused URLs."""


def assert_allowed_url(url: str) -> str:
    """Reject system-software URLs before any request is made."""
    if not url or not url.strip():
        raise SonyError("empty URL")
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise SonyError(f"unsupported URL scheme: {parsed.scheme or '<none>'}")
    for pattern in FORBIDDEN_URL_PATTERNS:
        if pattern.search(candidate):
            raise SonyError(
                "refused: this URL looks like PlayStation system software. "
                "Only game update packages are supported."
            )
    return candidate


def is_playstation_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.lower().endswith("playstation.net")


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def _replace_path_tail(url: str, tail_len: int, replacement: str) -> str:
    parsed = urlparse(url)
    new_path = parsed.path[: len(parsed.path) - tail_len] + replacement
    return urlunparse(parsed._replace(path=new_path))


_NUMBERED_PIECE_RE = re.compile(r"_(\d+)\.pkg$", re.I)


def to_manifest_url(url: str) -> Optional[str]:
    """Return the JSON manifest URL for a package URL, when it is derivable.

    * ``*.json``      -> itself
    * ``*_sc.pkg``    -> sibling ``.json`` (PS5: same ``/app/info/<rev>/f_<hash>/``)
    * legacy PS4 ``ppkgo`` layouts -> sibling ``.json`` for ``-DP.pkg``/``_0.pkg``

    PS5 pieces below ``/app/pkg/`` return ``None``: their manifest lives under a
    different revision and hash, so renaming the file would produce a 404.
    """
    candidate = url.strip()
    path = urlparse(candidate).path
    lowered = path.lower()

    if lowered.endswith(".json"):
        return candidate
    if lowered.endswith("_sc.pkg"):
        return _replace_path_tail(candidate, len("_sc.pkg"), ".json")

    legacy = "/ppkgo/" in lowered or "gs2." in (urlparse(candidate).hostname or "")
    if legacy:
        if lowered.endswith("-dp.pkg"):
            return _replace_path_tail(candidate, len("-DP.pkg"), ".json")
        match = _NUMBERED_PIECE_RE.search(path)
        if match:
            return _replace_path_tail(candidate, len(match.group(0)), ".json")
        if lowered.endswith(".pkg"):
            return _replace_path_tail(candidate, len(".pkg"), ".json")
    return None


def is_ps5_package_piece(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/app/pkg/" in path and path.endswith(".pkg")


def looks_like_version_xml(url: str) -> bool:
    return urlparse(url).path.lower().endswith("version.xml")


# ---------------------------------------------------------------------------
# version.xml
# ---------------------------------------------------------------------------

@dataclass
class SonyPackage:
    """One ``<package>`` entry of a ``version.xml``."""

    kind: str                      # "app" | "ac" (additional content)
    content_id: str
    content_version: str
    manifest_url: str
    required_firmware: Optional[str]
    digest: Optional[str] = None
    delta_url: Optional[str] = None
    system_ver: Optional[str] = None
    metadata_ver: Optional[str] = None
    name: Optional[str] = None

    @property
    def title_id(self) -> str:
        parts = self.content_id.split("-")
        return parts[1].split("_")[0] if len(parts) > 1 else ""

    @property
    def region_code(self) -> str:
        return self.content_id.split("-")[0] if "-" in self.content_id else ""


@dataclass
class VersionDocument:
    title_id: str
    packages: List[SonyPackage] = field(default_factory=list)

    @property
    def app_packages(self) -> List[SonyPackage]:
        return [p for p in self.packages if p.kind == "app"]

    @property
    def latest_app(self) -> Optional[SonyPackage]:
        apps = self.app_packages
        return apps[-1] if apps else None


def parse_version_xml(data: bytes | str) -> VersionDocument:
    """Parse a PS5 ``<title_patch>`` document.

    The real documents look like::

        <title_patch nptitleid="PPSA08338_00">
          <app_tag content_id="EP9000-PPSA08338_00-MARVELSPIDERMAN2">
            <package content_ver="01.004.003" digest="..." manifest_url="..."
                     system_ver="167837696" delta_url="..."/>
          </app_tag>
          <ac_tag ...>...</ac_tag>
        </title_patch>
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SonyError(f"malformed version.xml: {exc}") from exc

    if root.tag != "title_patch":
        raise SonyError(f"unexpected version.xml root element <{root.tag}>")

    title_id = (root.get("nptitleid") or "").split("_")[0]
    document = VersionDocument(title_id=title_id)

    for tag in root:
        if tag.tag not in ("app_tag", "ac_tag"):
            continue
        kind = "app" if tag.tag == "app_tag" else "ac"
        content_id = tag.get("content_id", "")
        entry_name = tag.get("name")
        for package in tag.findall("package"):
            manifest_url = (package.get("manifest_url") or "").strip()
            if not manifest_url:
                continue
            document.packages.append(
                SonyPackage(
                    kind=kind,
                    content_id=content_id,
                    content_version=normalize_content_version(package.get("content_ver")),
                    manifest_url=manifest_url,
                    required_firmware=system_ver_to_firmware(package.get("system_ver")),
                    digest=package.get("digest"),
                    delta_url=(package.get("delta_url") or None),
                    system_ver=package.get("system_ver"),
                    metadata_ver=package.get("metadata_ver"),
                    name=entry_name,
                )
            )

    if not document.packages:
        raise SonyError("version.xml contains no package with a manifest_url")
    return document


# ---------------------------------------------------------------------------
# JSON manifest
# ---------------------------------------------------------------------------

@dataclass
class ManifestPiece:
    index: int
    url: str
    offset: int
    size: int
    hash_value: Optional[str] = None
    hash_algo: Optional[str] = None


@dataclass
class PackageManifest:
    source_url: str
    total_size: int
    pieces: List[ManifestPiece]
    content_id: Optional[str] = None
    package_digest: Optional[str] = None

    @property
    def is_split(self) -> bool:
        return len(self.pieces) > 1


def _first(mapping: dict, *names, default=None):
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            value = lowered[name.lower()]
            if value is not None:
                return value
    return default


def _as_int(value, default=None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def normalize_hash(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(hex_hash, algorithm)``; the length selects the algorithm."""
    if not value:
        return None, None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]+", text or ""):
        return None, None
    algo = SUPPORTED_HASHES.get(len(text))
    if algo is None:
        return None, None
    return text, algo


def parse_manifest(payload: dict, source_url: str) -> PackageManifest:
    """Parse a Sony package manifest.

    fetchpkg only reads ``originalFileSize`` and ``pieces[].url/hashValue``.
    We additionally read offsets and sizes so pieces can be fetched in
    parallel and resumed, and we accept a few alternative key spellings so a
    minor Sony change does not break the parser outright.
    """
    if not isinstance(payload, dict):
        raise SonyError("manifest root is not a JSON object")

    raw_pieces = _first(payload, "pieces", "files", default=None)
    if not isinstance(raw_pieces, list) or not raw_pieces:
        raise SonyError("manifest contains no 'pieces' array")

    total_size = _as_int(_first(payload, "originalFileSize", "fileSize", "size"), 0) or 0

    pieces: List[ManifestPiece] = []
    running_offset = 0
    for index, raw in enumerate(raw_pieces):
        if not isinstance(raw, dict):
            raise SonyError(f"manifest piece {index} is not an object")
        url = _first(raw, "url", "downloadUrl", "href")
        if not url:
            raise SonyError(f"manifest piece {index} has no URL")
        url = urljoin(source_url, str(url).strip())
        assert_allowed_url(url)

        size = _as_int(_first(raw, "fileSize", "size", "length", "pieceSize", "downloadSize"))
        offset = _as_int(_first(raw, "fileOffset", "offset", "startOffset", "start"))
        hash_value, hash_algo = normalize_hash(_first(raw, "hashValue", "hash", "digest"))

        if offset is None:
            offset = running_offset
        if offset < 0:
            raise SonyError(f"manifest piece {index} has a negative offset")
        if size is not None:
            if size < 0:
                raise SonyError(f"manifest piece {index} has a negative size")
            running_offset = max(running_offset, offset + size)

        pieces.append(
            ManifestPiece(
                index=index,
                url=url,
                offset=offset,
                size=size if size is not None else -1,
                hash_value=hash_value,
                hash_algo=hash_algo,
            )
        )

    if total_size <= 0:
        total_size = running_offset
    if running_offset and running_offset > total_size:
        raise SonyError(
            f"manifest pieces span {running_offset} bytes, beyond originalFileSize {total_size}"
        )

    content_id = _first(payload, "contentId", "content_id")
    digest, _ = normalize_hash(_first(payload, "packageDigest", "digest"))
    return PackageManifest(
        source_url=source_url,
        total_size=total_size,
        pieces=pieces,
        content_id=str(content_id) if content_id else None,
        package_digest=digest,
    )


def extract_playstation_links(text: str) -> List[str]:
    """Pull Sony package/manifest URLs out of arbitrary HTML or JSON text.

    This is the fallback used when a metadata provider changes its markup:
    whatever the surrounding structure looks like, the official links keep the
    same shape.
    """
    found: List[str] = []
    seen = set()
    for match in PLAYSTATION_LINK_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;\\")
        url = url.replace("\\/", "/").replace("&amp;", "&")
        path = urlparse(url).path.lower()
        if not (path.endswith(".json") or path.endswith(".pkg") or path.endswith("version.xml")):
            continue
        if any(pattern.search(url) for pattern in FORBIDDEN_URL_PATTERNS):
            continue
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found
