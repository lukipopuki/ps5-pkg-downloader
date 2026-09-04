"""Output layout on disk.

    /downloads/
    └── PPSA08338/
        ├── metadata.json
        └── 01.004.003/
            ├── metadata.json
            └── PPSA08338_01.004.003.pkg

Additional content lands in ``<TITLE_ID>/dlc/<CONTENT_ID>/<version>/``.  While a
download is unfinished the payload is a ``.pkg.part`` next to its final name;
it is renamed atomically once every hash has been verified, so a ``.pkg`` in
the download directory is always a complete, verified package.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_component(value: str, fallback: str = "unknown") -> str:
    cleaned = _UNSAFE.sub("_", (value or "").strip()).strip("._-")
    return cleaned or fallback


@dataclass
class OutputPaths:
    directory: Path
    final_path: Path
    temp_path: Path
    title_directory: Path

    @property
    def state_path(self) -> Path:
        return self.temp_path.with_suffix(self.temp_path.suffix + ".json")


def build_paths(
    download_dir: Path,
    title_id: str,
    content_ver: str,
    *,
    kind: str = "app",
    content_id: str = "",
) -> OutputPaths:
    title_component = safe_component(title_id, "unknown_title")
    version_component = safe_component(content_ver, "unknown_version")
    title_directory = download_dir / title_component

    if kind == "ac":
        content_component = safe_component(content_id, "dlc")
        directory = title_directory / "dlc" / content_component / version_component
        file_name = f"{content_component}_{version_component}.pkg"
    else:
        directory = title_directory / version_component
        file_name = f"{title_component}_{version_component}.pkg"

    final_path = directory / file_name
    return OutputPaths(
        directory=directory,
        final_path=final_path,
        temp_path=final_path.with_name(final_path.name + ".part"),
        title_directory=title_directory,
    )


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON through a temp file + rename so readers never see half a file."""
    ensure_directory(path.parent)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o644)  # mkstemp creates 0600; sidecars stay readable
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def allocate(path: Path, size: int, preallocate: bool = True) -> None:
    """Create the output file and give it its final logical length.

    ``truncate`` produces a sparse file on ext4/XFS/BTRFS, so no space is
    consumed until data is actually written, and pieces can be written at
    their own offsets in any order.
    """
    ensure_directory(path.parent)
    with open(path, "a+b") as handle:
        if preallocate and size > 0:
            current = handle.seek(0, os.SEEK_END)
            if current < size:
                handle.truncate(size)


def finalize(temp_path: Path, final_path: Path) -> None:
    """Atomically publish a finished download."""
    os.replace(temp_path, final_path)


def fsync_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def cleanup_partial(paths: OutputPaths) -> None:
    """Remove the partial payload and its sidecar (used on cancel)."""
    for candidate in (paths.temp_path, paths.state_path):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove %s: %s", candidate, exc)


def prune_empty_dirs(directory: Path, stop_at: Path) -> None:
    """Walk up from ``directory`` removing empty folders (never past ``stop_at``)."""
    current = directory
    try:
        stop_at = stop_at.resolve()
        current = current.resolve()
    except OSError:
        return
    while current != stop_at and stop_at in current.parents:
        try:
            next(current.iterdir())
            return  # not empty
        except StopIteration:
            pass
        except OSError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def free_space(path: Path) -> Optional[int]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        stat = os.statvfs(probe)
    except (OSError, AttributeError):
        return None
    return stat.f_bavail * stat.f_frsize
