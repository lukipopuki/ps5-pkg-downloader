"""PROSPEROPatches parsing.

Every parser is driven by the rules file, so these tests double as a check
that the shipped default rules still match the markup we expect.
"""

import json

import pytest

from app.providers.prospero import (
    parse_additional_content,
    parse_patch_list,
    parse_search_payload,
    parse_size,
    parse_title_links,
    parse_title_page,
    is_title_id,
    strip_tags,
)
from app.providers.rules import Rules, default_rules, load_rules


@pytest.fixture
def rules() -> Rules:
    return default_rules()


def test_parse_title_page(fixture_dir, rules):
    markup = (fixture_dir / "prospero_title.html").read_text(encoding="utf-8")
    page = parse_title_page(markup, "PPSA08338", rules)

    assert page.name == "Marvel's Spider-Man 2"
    assert page.data_key == "7b1f4c9de2a08356"
    assert page.content_id == "EP9000-PPSA08338_00-MARVELSPIDERMAN2"
    assert page.publisher == "Sony Interactive Entertainment"
    assert page.publisher_id == "EP9000"
    assert page.region == "Europe"
    assert page.icon_url.endswith("icon0.webp")
    assert page.banner_url.endswith("pic0.webp")
    assert "game updates" in page.description.lower()


def test_parse_title_page_of_unknown_title(rules):
    page = parse_title_page("<title>PROSPEROPatches.com</title>", "PPSA99999", rules)
    assert page.is_empty()


def test_parse_patch_list(rules):
    payload = {
        "success": True,
        "lastupdated": "2024-01-18",
        "count": 2,
        "patches": [
            {
                "content_ver": "01.004.003",
                "filesize": "126.1 GB",
                "required_firmware": "10.01",
                "import_date": "2024-01-18",
                "is_latest": True,
                "changelog_preview": "<p>Bug fixes</p>",
                "keyset": {"patch": "aaa", "details": "bbb", "changeinfo": "ccc"},
            },
            {"content_ver": "1.4.0", "filesize": 125000000000, "required_firmware": "FW 10.01"},
        ],
    }
    patches, last_updated = parse_patch_list(payload, rules)

    assert last_updated == "2024-01-18"
    assert [p.content_ver for p in patches] == ["01.004.003", "01.004.000"]
    assert patches[0].file_size == 126_100_000_000
    assert patches[0].is_latest is True
    assert patches[0].changelog == "Bug fixes"
    assert patches[0].keyset_details == "bbb"
    assert patches[1].required_firmware == "10.01"


def test_parse_patch_list_tolerates_failure_responses(rules):
    assert parse_patch_list({"success": False}, rules) == ([], "")
    assert parse_patch_list({}, rules) == ([], "")
    assert parse_patch_list("not json", rules) == ([], "")
    assert parse_patch_list({"patches": ["nonsense", {"no_version": 1}]}, rules) == ([], "")


def test_parse_additional_content(rules):
    payload = {
        "success": True,
        "items": [
            {
                "name": "Spider-Man 2 DLC",
                "contentid": "EP9000-PPSA08338_00-SPIDERMAN2DLC001",
                "content_ver": "01.000.000",
                "filesize": "2.4 GB",
                "required_firmware": "02.00",
                "icon": "https://cdn/icon.webp",
                "key": "kkk",
            }
        ],
    }
    items = parse_additional_content(payload, rules)
    assert items[0].content_id.endswith("DLC001")
    assert items[0].file_size == 2_400_000_000


def test_search_result_extraction_from_html(fixture_dir, rules):
    markup = (fixture_dir / "prospero_search.html").read_text(encoding="utf-8")
    results = parse_title_links(markup, rules, "search")
    assert [r.title_id for r in results] == ["PPSA08338", "PPSA01411"]
    assert results[0].name == "Marvel's Spider-Man 2"


def test_search_result_extraction_from_json(rules):
    payload = {
        "success": True,
        "data": {"hits": [
            {"titleid": "PPSA08338", "name": "Marvel's Spider-Man 2", "region": "EU"},
            {"titleid": "nope", "name": "ignored"},
        ]},
    }
    results = parse_search_payload(payload, rules)
    assert len(results) == 1
    assert results[0].title_id == "PPSA08338"
    assert results[0].region == "EU"


def test_region_links_are_parsed(rules):
    markup = '<a href="/PPSA08339" class="x">USA</a><a href="/PPSA08340">Japan</a>'
    results = parse_title_links(markup, rules, "regions")
    assert [(r.title_id, r.name) for r in results] == [("PPSA08339", "USA"), ("PPSA08340", "Japan")]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("126.1 GB", 126_100_000_000),
        ("12,5 GiB", int(12.5 * 1024 ** 3)),
        ("980 MB", 980_000_000),
        (4096, 4096),
        ("4096", 4096),
        ("unknown", None),
        (None, None),
    ],
)
def test_parse_size(value, expected):
    assert parse_size(value) == expected


def test_title_id_validation():
    assert is_title_id("PPSA08338")
    assert not is_title_id("ppsa08338".upper() + "0")
    assert not is_title_id("spider-man")


def test_strip_tags_unescapes_entities():
    assert strip_tags("<b>Marvel&#039;s</b>  Spider-Man") == "Marvel's Spider-Man"


def test_custom_rules_file_overrides_defaults(tmp_path):
    """A site change is repaired by editing YAML, not by changing code."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        "version: 2\n"
        "title_page:\n"
        "  patterns:\n"
        "    data_key:\n"
        "      - 'data-token=\"([a-z0-9]+)\"'\n"
        "    title:\n"
        "      - '<h1>([^<]*)</h1>'\n",
        encoding="utf-8",
    )
    rules = load_rules(path)
    assert rules.version == 2
    page = parse_title_page('<h1>New Layout</h1><span data-token="abc123"></span>', "PPSA00001", rules)
    assert page.name == "New Layout"
    assert page.data_key == "abc123"


def test_broken_rules_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("::: not yaml :::", encoding="utf-8")
    rules = load_rules(path)
    assert rules.section("title_page")  # built-in defaults are in place


def test_default_rules_shape():
    rules = default_rules()
    for section in ("title_page", "patches", "additional_content", "regions", "search", "link_resolution"):
        assert rules.section(section), f"{section} missing from default rules"
    assert all(r.method in ("GET", "POST") for r in rules.requests("patches"))
