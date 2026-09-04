"""Firmware and version arithmetic.

The system_ver values below are taken from real PS5 version.xml documents.
"""

import pytest

from app.versions import (
    compare_versions,
    is_firmware_compatible,
    normalize_content_version,
    parse_version,
    system_ver_to_firmware,
)


@pytest.mark.parametrize(
    "system_ver,expected",
    [
        (33554432, "02.00"),    # 0x02000000
        (38797312, "02.50"),    # 0x02500000
        (51380224, "03.10"),    # 0x03100000
        (167772160, "10.00"),   # 0x0a000000 - the naive decimal decode breaks here
        (167837696, "10.01"),   # 0x0a010000
        (207618048, "12.60"),   # 0x0c600000
        ("167837696", "10.01"),
        ("0x0a010000", "10.01"),
    ],
)
def test_system_ver_decoding(system_ver, expected):
    assert system_ver_to_firmware(system_ver) == expected


@pytest.mark.parametrize("value", [None, "", "not a number", -1])
def test_system_ver_rejects_garbage(value):
    assert system_ver_to_firmware(value) is None


def test_firmware_major_above_nine_sorts_correctly():
    assert compare_versions("10.01", "9.60") == 1
    assert compare_versions("02.50", "10.00") == -1


def test_firmware_compatibility():
    assert is_firmware_compatible("10.01", "12.60") is True
    assert is_firmware_compatible("12.60", "12.60") is True
    assert is_firmware_compatible("13.00", "12.60") is False
    # No maximum configured means everything passes.
    assert is_firmware_compatible("13.00", "") is True
    # An unknown requirement is never silently treated as compatible.
    assert is_firmware_compatible(None, "12.60") is None
    assert is_firmware_compatible("unknown", "12.60") is None


def test_content_version_normalisation():
    assert normalize_content_version("1.4.3") == "01.004.003"
    assert normalize_content_version("01.004.003") == "01.004.003"
    assert normalize_content_version("") == ""


def test_parse_version():
    assert parse_version("01.004.003") == (1, 4, 3)
    assert parse_version("10.01") == (10, 1)
    assert parse_version("garbage") is None
