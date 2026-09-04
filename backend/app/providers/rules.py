"""Scraping rules for the PROSPEROPatches provider.

PROSPEROPatches has no documented public API, so every request path, request
body and regular expression the provider uses lives in a YAML file instead of
in Python code.  ``prospero_rules.default.yaml`` next to this module holds the
shipped defaults; on first start they are copied to ``/config`` where they can
be edited and reloaded at runtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

log = logging.getLogger(__name__)

DEFAULT_RULES_PATH = Path(__file__).with_name("prospero_rules.default.yaml")


@dataclass
class RequestRule:
    method: str = "GET"
    path: str = "/"
    params: Dict[str, str] = field(default_factory=dict)
    encoding: str = "query"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RequestRule":
        return cls(
            method=str(data.get("method", "GET")).upper(),
            path=str(data.get("path", "/")),
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            encoding=str(data.get("encoding", "query")).lower(),
        )


def _compile_all(raw_patterns: List[Any], where: str) -> List[re.Pattern]:
    compiled = []
    for raw in raw_patterns:
        try:
            compiled.append(re.compile(str(raw)))
        except re.error as exc:
            log.error("invalid regex in %s (%s): %s", where, raw, exc)
    return compiled


class Rules:
    """Typed accessor around the raw YAML document."""

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self.data = data or {}
        self.source = source

    def section(self, name: str) -> Dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def requests(self, section: str) -> List[RequestRule]:
        raw = self.section(section).get("requests") or []
        rules: List[RequestRule] = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    rules.append(RequestRule.from_dict(item))
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("ignoring malformed request rule in %s: %s", section, exc)
        return rules

    def patterns(self, section: str, name: str) -> List[re.Pattern]:
        raw = self.section(section).get("patterns", {}).get(name) or []
        if isinstance(raw, str):
            raw = [raw]
        return _compile_all(list(raw), f"{section}.patterns.{name}")

    def pattern(self, section: str, name: str = "pattern") -> Optional[re.Pattern]:
        raw = self.section(section).get(name)
        if not raw:
            return None
        compiled = _compile_all([raw], f"{section}.{name}")
        return compiled[0] if compiled else None

    def regex_list(self, section: str, name: str) -> List[re.Pattern]:
        raw = self.section(section).get(name) or []
        if isinstance(raw, str):
            raw = [raw]
        return _compile_all(list(raw), f"{section}.{name}")

    def fields(self, section: str, key: str = "fields") -> Dict[str, List[str]]:
        raw = self.section(section).get(key) or {}
        result: Dict[str, List[str]] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if isinstance(value, str):
                    result[str(name)] = [value]
                elif isinstance(value, list):
                    result[str(name)] = [str(v) for v in value]
        return result

    def value(self, section: str, name: str, default: Any = None) -> Any:
        return self.section(section).get(name, default)

    @property
    def headers(self) -> Dict[str, str]:
        raw = self.data.get("headers") or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def version(self) -> int:
        try:
            return int(self.data.get("version", 1))
        except (TypeError, ValueError):
            return 1


def _read_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        log.error("Invalid YAML in %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.error("Rules file %s is not a YAML mapping", path)
        return None
    return data


def default_rules_text() -> str:
    return DEFAULT_RULES_PATH.read_text(encoding="utf-8")


def default_rules() -> Rules:
    data = _read_yaml(DEFAULT_RULES_PATH)
    if data is None:  # pragma: no cover - only if the package is broken
        raise RuntimeError(f"built-in rules file missing or invalid: {DEFAULT_RULES_PATH}")
    return Rules(data, source=DEFAULT_RULES_PATH)


def ensure_rules_file(path: Path) -> None:
    """Write the shipped defaults to ``path`` when it does not exist yet."""
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_rules_text(), encoding="utf-8")
        log.info("Created default PROSPERO rules file at %s", path)
    except OSError as exc:
        log.warning("Could not write default rules file %s: %s", path, exc)


def load_rules(path: Path) -> Rules:
    """Load rules from ``path``; fall back to the built-in defaults."""
    data = _read_yaml(path)
    if data is None:
        log.warning("Using built-in PROSPERO rules (could not load %s)", path)
        return default_rules()
    return Rules(data, source=path)
