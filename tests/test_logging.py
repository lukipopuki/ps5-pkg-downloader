"""Guards against a whole class of runtime crash.

``logging`` refuses an ``extra`` dict that carries a reserved LogRecord
attribute and raises KeyError from inside the log call - which turns a plain
log line into a failed request. A grep-level test is worth it here because the
mistake is invisible until that exact code path runs.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"

RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


def collect_extra_keys():
    for source in sorted(BACKEND.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        yield source.relative_to(BACKEND), node.lineno, key.value


def test_no_log_extra_uses_a_reserved_record_attribute():
    offenders = [
        f"{path}:{line} uses extra key {key!r}"
        for path, line, key in collect_extra_keys()
        if key in RESERVED
    ]
    assert not offenders, "logging would raise KeyError at runtime:\n" + "\n".join(offenders)


def test_the_guard_actually_catches_the_mistake():
    """Prove the failure mode is real, so the test above is not decoration.

    Note the level check: the record is only built when the level is enabled,
    so this crash hides itself at LOG_LEVEL=WARNING and appears at the default
    INFO - which is exactly what makes it easy to ship.
    """
    logger = logging.getLogger("guard-check")
    logger.setLevel(logging.INFO)
    with pytest.raises(KeyError):
        logger.info("boom", extra={"name": "collides with LogRecord.name"})
    logger.info("fine", extra={"title_name": "safe"})
