"""Firmware and content version helpers.

Sony encodes the required system software version of a package as a 32 bit
integer in ``version.xml`` (attribute ``system_ver``).  The value is BCD-ish:
``0x0A010000`` is firmware ``10.01`` and ``0x02500000`` is ``2.50``.  Decoding
by formatting the integer as eight hex digits and reading the first two pairs
keeps working for firmware majors >= 10, which a naive decimal conversion gets
wrong.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_VERSION_RE = re.compile(r"^\s*v?(\d{1,3})(?:[.,](\d{1,3}))?(?:[.,](\d{1,3}))?")


def system_ver_to_firmware(system_ver: object) -> Optional[str]:
    """Convert a ``system_ver`` attribute into a ``MM.mm`` firmware string."""
    if system_ver is None:
        return None
    if isinstance(system_ver, str):
        text = system_ver.strip()
        if not text:
            return None
        try:
            value = int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    elif isinstance(system_ver, (int, float)):
        value = int(system_ver)
    else:
        return None

    if value < 0:
        return None
    digits = f"{value:08x}"
    if len(digits) > 8:  # absurd value, refuse to guess
        return None
    # The major nibble pair is hex (0x0a -> 10), the minor pair is plain BCD.
    major, minor = digits[0:2], digits[2:4]
    if not re.fullmatch(r"[0-9a-f]{2}", major) or not minor.isdigit():
        return None
    return f"{int(major, 16):02d}.{minor}"


def parse_version(value: Optional[str]) -> Optional[Tuple[int, ...]]:
    """Parse ``10.01``/``01.004.003``/``10.01.00.00`` into a comparable tuple."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = re.split(r"[.,\-]", text)
    numbers = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            match = re.match(r"^\d+", part)
            if not match:
                break
            part = match.group(0)
        numbers.append(int(part))
    if not numbers:
        return None
    return tuple(numbers)


def _pad(a: Tuple[int, ...], b: Tuple[int, ...]) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    size = max(len(a), len(b))
    return a + (0,) * (size - len(a)), b + (0,) * (size - len(b))


def compare_versions(left: Optional[str], right: Optional[str]) -> Optional[int]:
    """Return -1/0/1, or None when either side cannot be parsed."""
    a, b = parse_version(left), parse_version(right)
    if a is None or b is None:
        return None
    a, b = _pad(a, b)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_firmware_compatible(required: Optional[str], maximum: Optional[str]) -> Optional[bool]:
    """True when ``required`` <= ``maximum``.

    ``None`` means "unknown" - an unparsable or missing value must never be
    silently treated as compatible.
    """
    if not maximum:
        return True
    result = compare_versions(required, maximum)
    if result is None:
        return None
    return result <= 0


def normalize_content_version(value: Optional[str]) -> str:
    """Normalise ``1.4.3`` / ``01.004.003`` into the canonical Sony form."""
    if not value:
        return ""
    text = str(value).strip()
    parts = parse_version(text)
    if parts is None:
        return re.sub(r"[^A-Za-z0-9._-]", "_", text)
    if len(parts) == 3:
        return f"{parts[0]:02d}.{parts[1]:03d}.{parts[2]:03d}"
    return text
