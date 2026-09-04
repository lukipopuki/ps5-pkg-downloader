"""Parsing of Sony documents and URL handling."""

import json

import pytest

from app.providers import sony
from app.providers.sony import SonyError


def test_parse_version_xml(fixture_dir):
    document = sony.parse_version_xml((fixture_dir / "version_ppsa08338.xml").read_bytes())

    assert document.title_id == "PPSA08338"
    assert len(document.packages) == 2

    app = document.latest_app
    assert app.kind == "app"
    assert app.content_version == "01.004.003"
    assert app.required_firmware == "10.01"
    assert app.content_id == "EP9000-PPSA08338_00-MARVELSPIDERMAN2"
    assert app.title_id == "PPSA08338"
    assert app.region_code == "EP9000"
    assert app.manifest_url.endswith("MARVELSPIDERMAN2.json")
    assert app.delta_url and app.delta_url.endswith("-DP.pkg")

    dlc = [p for p in document.packages if p.kind == "ac"]
    assert dlc[0].content_version == "01.000.000"
    assert dlc[0].required_firmware == "02.00"


def test_parse_version_xml_rejects_garbage():
    with pytest.raises(SonyError):
        sony.parse_version_xml(b"<html>nope</html>")
    with pytest.raises(SonyError):
        sony.parse_version_xml(b"not xml at all")


def test_parse_manifest(fixture_dir):
    payload = json.loads((fixture_dir / "manifest_split.json").read_text())
    manifest = sony.parse_manifest(payload, "https://sgst.prod.dl.playstation.net/x/TEST.json")

    assert manifest.total_size == 12884901888
    assert manifest.is_split
    assert [p.offset for p in manifest.pieces] == [0, 4294967296, 8589934592]
    # Hash algorithm is selected by digest length, exactly like fetchpkg does.
    assert manifest.pieces[0].hash_algo == "sha256"
    assert manifest.pieces[1].hash_algo == "sha1"
    assert manifest.package_digest


def test_parse_manifest_infers_offsets_when_absent():
    payload = {
        "originalFileSize": 300,
        "pieces": [
            {"url": "https://x.playstation.net/a_0.pkg", "fileSize": 100},
            {"url": "https://x.playstation.net/a_1.pkg", "fileSize": 200},
        ],
    }
    manifest = sony.parse_manifest(payload, "https://x.playstation.net/a.json")
    assert [p.offset for p in manifest.pieces] == [0, 100]


def test_parse_manifest_rejects_inconsistent_sizes():
    payload = {
        "originalFileSize": 10,
        "pieces": [{"url": "https://x.playstation.net/a_0.pkg", "fileOffset": 0, "fileSize": 100}],
    }
    with pytest.raises(SonyError):
        sony.parse_manifest(payload, "https://x.playstation.net/a.json")


def test_parse_manifest_requires_pieces():
    with pytest.raises(SonyError):
        sony.parse_manifest({"originalFileSize": 1}, "https://x/a.json")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://h/app/info/1/f_a/X.json", "https://h/app/info/1/f_a/X.json"),
        ("https://h/app/info/1/f_a/X_sc.pkg", "https://h/app/info/1/f_a/X.json"),
        # PS4 layout: the manifest really is a sibling of the package
        ("http://gs2.ww.prod.dl.playstation.net/gs2/ppkgo/prod/C_00/9/f_a/f/Y-DP.pkg",
         "http://gs2.ww.prod.dl.playstation.net/gs2/ppkgo/prod/C_00/9/f_a/f/Y.json"),
        ("http://gs2.ww.prod.dl.playstation.net/gs2/ppkgo/prod/C_00/9/f_a/f/Y_0.pkg",
         "http://gs2.ww.prod.dl.playstation.net/gs2/ppkgo/prod/C_00/9/f_a/f/Y.json"),
    ],
)
def test_manifest_url_normalisation(url, expected):
    assert sony.to_manifest_url(url) == expected


def test_ps5_package_pieces_are_never_guessed():
    # The manifest for these lives under a different revision and hash, so
    # renaming the file would produce a 404.
    url = "https://gst.prod.dl.playstation.net/gst/prod/00/T_00/app/pkg/17/f_x/C_0.pkg"
    assert sony.to_manifest_url(url) is None
    assert sony.is_ps5_package_piece(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://x/PS5UPDATE.PUP",
        "https://cdn/ps4/updater/PS4UPDATE.PUP",
        "https://x/system-update/thing.json",
        "https://x/file.pup",
    ],
)
def test_system_software_urls_are_refused(url):
    with pytest.raises(SonyError):
        sony.assert_allowed_url(url)


def test_non_http_urls_are_refused():
    with pytest.raises(SonyError):
        sony.assert_allowed_url("file:///etc/passwd")
    with pytest.raises(SonyError):
        sony.assert_allowed_url("")


def test_extract_playstation_links_filters_noise():
    markup = """
      <a href="https://sgst.prod.dl.playstation.net/sgst/prod/00/T/app/info/1/f_a/C.json">manifest</a>
      <a href="https://gst.prod.dl.playstation.net/gst/prod/00/T/app/pkg/1/f_b/C_0.pkg">piece</a>
      <a href="https://example.com/other.json">unrelated</a>
      <a href="https://cdn.playstation.net/PS5UPDATE.PUP">system software</a>
      <img src="https://cdn.playstation.net/image.webp">
    """
    links = sony.extract_playstation_links(markup)
    assert links == [
        "https://sgst.prod.dl.playstation.net/sgst/prod/00/T/app/info/1/f_a/C.json",
        "https://gst.prod.dl.playstation.net/gst/prod/00/T/app/pkg/1/f_b/C_0.pkg",
    ]


def test_extract_playstation_links_handles_json_escaping():
    payload = json.dumps({"link": "https://sgst.prod.dl.playstation.net/a/b/C.json"})
    assert sony.extract_playstation_links(payload) == [
        "https://sgst.prod.dl.playstation.net/a/b/C.json"
    ]


def test_normalize_hash_selects_algorithm():
    assert sony.normalize_hash("0x" + "ab" * 32) == ("ab" * 32, "sha256")
    assert sony.normalize_hash("AB" * 20) == ("ab" * 20, "sha1")
    assert sony.normalize_hash("zz") == (None, None)
    assert sony.normalize_hash(None) == (None, None)
