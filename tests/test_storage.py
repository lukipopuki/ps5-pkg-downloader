"""Output layout, atomic publishing and cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.download import storage


def test_layout_matches_the_documented_structure(tmp_path):
    paths = storage.build_paths(tmp_path, "PPSA08338", "01.004.003")
    assert paths.final_path == tmp_path / "PPSA08338" / "01.004.003" / "PPSA08338_01.004.003.pkg"
    assert paths.temp_path.name == "PPSA08338_01.004.003.pkg.part"
    assert paths.title_directory == tmp_path / "PPSA08338"


def test_dlc_gets_its_own_subtree(tmp_path):
    paths = storage.build_paths(
        tmp_path, "PPSA08338", "01.000.000", kind="ac",
        content_id="EP9000-PPSA08338_00-SPIDERMAN2DLC001",
    )
    assert paths.final_path == (
        tmp_path / "PPSA08338" / "dlc" / "EP9000-PPSA08338_00-SPIDERMAN2DLC001" /
        "01.000.000" / "EP9000-PPSA08338_00-SPIDERMAN2DLC001_01.000.000.pkg"
    )


def test_path_components_cannot_escape_the_download_directory(tmp_path):
    paths = storage.build_paths(tmp_path, "../../etc", "../passwd")
    assert tmp_path in paths.final_path.parents
    assert ".." not in str(paths.final_path)


def test_atomic_json_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "metadata.json"
    storage.write_json_atomic(target, {"b": 1, "a": 2})
    assert json.loads(target.read_text()) == {"a": 2, "b": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["metadata.json"]

    storage.write_json_atomic(target, {"a": 3})
    assert json.loads(target.read_text()) == {"a": 3}


def test_allocate_creates_a_sparse_file_of_the_right_length(tmp_path):
    target = tmp_path / "out.pkg.part"
    storage.allocate(target, 5_000_000)
    assert target.stat().st_size == 5_000_000
    # Sparse: the logical size is not backed by blocks yet.
    assert target.stat().st_blocks * 512 < 5_000_000

    # Allocating again must never shrink an existing partial file.
    with open(target, "r+b") as handle:
        handle.seek(4_999_999)
        handle.write(b"x")
    storage.allocate(target, 5_000_000)
    assert target.stat().st_size == 5_000_000


def test_finalize_is_atomic_rename(tmp_path):
    temp = tmp_path / "a.pkg.part"
    final = tmp_path / "a.pkg"
    temp.write_bytes(b"data")
    storage.finalize(temp, final)
    assert final.read_bytes() == b"data"
    assert not temp.exists()


def test_cleanup_removes_partials_and_empty_directories(tmp_path):
    paths = storage.build_paths(tmp_path, "PPSA08338", "01.004.003")
    storage.ensure_directory(paths.directory)
    paths.temp_path.write_bytes(b"partial")
    paths.state_path.write_text("{}")

    storage.cleanup_partial(paths)
    assert not paths.temp_path.exists()
    assert not paths.state_path.exists()

    storage.prune_empty_dirs(paths.directory, tmp_path)
    assert not paths.directory.exists()
    assert not paths.title_directory.exists()
    assert tmp_path.exists()


def test_prune_stops_at_a_non_empty_directory(tmp_path):
    paths = storage.build_paths(tmp_path, "PPSA08338", "01.004.003")
    storage.ensure_directory(paths.directory)
    (paths.title_directory / "metadata.json").write_text("{}")
    storage.prune_empty_dirs(paths.directory, tmp_path)
    assert paths.title_directory.exists()
    assert not paths.directory.exists()


def test_free_space_probes_the_nearest_existing_parent(tmp_path):
    assert storage.free_space(tmp_path / "does" / "not" / "exist") > 0
