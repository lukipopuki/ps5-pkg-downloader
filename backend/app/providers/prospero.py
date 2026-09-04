"""PROSPEROPatches metadata provider.

The site is a normal web application without a public API contract, so this
module is deliberately thin: it performs the requests described in the rules
file and converts whatever comes back into our own data classes.  Every parsing
step is a pure function so it can be unit tested against saved fixtures, and
every network step degrades to an empty result instead of raising, so one
broken endpoint never takes the whole lookup down.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from ..config import Settings
from ..http import raise_for_retryable, with_retries
from ..versions import normalize_content_version
from . import sony
from .rules import RequestRule, Rules

log = logging.getLogger(__name__)

TITLE_ID_RE = re.compile(r"^[A-Z]{4}[0-9]{5}$")
_TAG_RE = re.compile(r"<[^>]+>")
_SIZE_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(B|KB|KIB|MB|MIB|GB|GIB|TB|TIB)\b", re.I)
_UNITS = {
    "B": 1,
    "KB": 1000, "KIB": 1024,
    "MB": 1000 ** 2, "MIB": 1024 ** 2,
    "GB": 1000 ** 3, "GIB": 1024 ** 3,
    "TB": 1000 ** 4, "TIB": 1024 ** 4,
}


class ProsperoError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def is_title_id(value: str) -> bool:
    return bool(TITLE_ID_RE.match((value or "").strip().upper()))


def strip_tags(markup: str) -> str:
    text = _TAG_RE.sub(" ", markup or "")
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_size(value: Any) -> Optional[int]:
    """Accept ``126100000000``, ``"126.1 GB"`` or ``"12,5 GiB"``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = _SIZE_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = _UNITS.get(match.group(2).upper())
    if unit is None:
        return None
    return int(number * unit)


def _first_group(patterns: Iterable[re.Pattern], text: str) -> Optional[str]:
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def _dig(item: Dict[str, Any], path: str) -> Any:
    """Read ``a.b.c`` out of nested dicts, case insensitively."""
    current: Any = item
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        lowered = {str(k).lower(): v for k, v in current.items()}
        if part.lower() not in lowered:
            return None
        current = lowered[part.lower()]
    return current


def _pick(item: Dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = _dig(item, name)
        if value is not None and value != "":
            return value
    return None


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass
class TitlePage:
    title_id: str
    name: str = ""
    description: str = ""
    region: str = ""
    content_id: str = ""
    publisher: str = ""
    publisher_id: str = ""
    icon_url: str = ""
    banner_url: str = ""
    data_key: Optional[str] = None

    def is_empty(self) -> bool:
        return not (self.name or self.content_id or self.data_key)


@dataclass
class Patch:
    content_ver: str
    file_size: Optional[int] = None
    required_firmware: Optional[str] = None
    import_date: Optional[str] = None
    is_latest: bool = False
    changelog: str = ""
    keyset_patch: str = ""
    keyset_details: str = ""
    keyset_changeinfo: str = ""
    manifest_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content_ver": self.content_ver,
            "file_size": self.file_size,
            "required_firmware": self.required_firmware,
            "import_date": self.import_date,
            "is_latest": self.is_latest,
            "changelog": self.changelog,
            "keyset_patch": self.keyset_patch,
            "keyset_details": self.keyset_details,
            "keyset_changeinfo": self.keyset_changeinfo,
            "manifest_url": self.manifest_url,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Patch":
        return cls(
            content_ver=str(data.get("content_ver") or ""),
            file_size=data.get("file_size"),
            required_firmware=data.get("required_firmware"),
            import_date=data.get("import_date"),
            is_latest=bool(data.get("is_latest")),
            changelog=str(data.get("changelog") or ""),
            keyset_patch=str(data.get("keyset_patch") or ""),
            keyset_details=str(data.get("keyset_details") or ""),
            keyset_changeinfo=str(data.get("keyset_changeinfo") or ""),
            manifest_url=data.get("manifest_url"),
        )


@dataclass
class AdditionalContent:
    name: str = ""
    content_id: str = ""
    content_ver: str = ""
    file_size: Optional[int] = None
    required_firmware: Optional[str] = None
    icon_url: str = ""
    key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "content_id": self.content_id,
            "content_ver": self.content_ver,
            "file_size": self.file_size,
            "required_firmware": self.required_firmware,
            "icon_url": self.icon_url,
            "key": self.key,
        }


@dataclass
class SearchResult:
    title_id: str
    name: str = ""
    region: str = ""
    icon_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title_id": self.title_id,
            "name": self.name,
            "region": self.region,
            "icon_url": self.icon_url,
        }


@dataclass
class TitleDetails:
    page: TitlePage
    patches: List[Patch] = field(default_factory=list)
    additional_content: List[AdditionalContent] = field(default_factory=list)
    regions: List[SearchResult] = field(default_factory=list)
    last_updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title_id": self.page.title_id,
            "name": self.page.name,
            "description": self.page.description,
            "region": self.page.region,
            "content_id": self.page.content_id,
            "publisher": self.page.publisher,
            "publisher_id": self.page.publisher_id,
            "icon_url": self.page.icon_url,
            "banner_url": self.page.banner_url,
            "data_key": self.page.data_key,
            "last_updated": self.last_updated,
            "patches": [p.to_dict() for p in self.patches],
            "additional_content": [a.to_dict() for a in self.additional_content],
            "regions": [r.to_dict() for r in self.regions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TitleDetails":
        page = TitlePage(
            title_id=str(data.get("title_id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            region=str(data.get("region") or ""),
            content_id=str(data.get("content_id") or ""),
            publisher=str(data.get("publisher") or ""),
            publisher_id=str(data.get("publisher_id") or ""),
            icon_url=str(data.get("icon_url") or ""),
            banner_url=str(data.get("banner_url") or ""),
            data_key=data.get("data_key"),
        )
        return cls(
            page=page,
            patches=[Patch.from_dict(p) for p in data.get("patches") or []],
            additional_content=[
                AdditionalContent(
                    name=str(a.get("name") or ""),
                    content_id=str(a.get("content_id") or ""),
                    content_ver=str(a.get("content_ver") or ""),
                    file_size=a.get("file_size"),
                    required_firmware=a.get("required_firmware"),
                    icon_url=str(a.get("icon_url") or ""),
                    key=str(a.get("key") or ""),
                )
                for a in data.get("additional_content") or []
            ],
            regions=[
                SearchResult(
                    title_id=str(r.get("title_id") or ""),
                    name=str(r.get("name") or ""),
                    region=str(r.get("region") or ""),
                    icon_url=str(r.get("icon_url") or ""),
                )
                for r in data.get("regions") or []
            ],
            last_updated=str(data.get("last_updated") or ""),
        )


# ---------------------------------------------------------------------------
# parsers (pure - unit tested against fixtures)
# ---------------------------------------------------------------------------

def parse_title_page(markup: str, title_id: str, rules: Rules) -> TitlePage:
    page = TitlePage(title_id=title_id)

    raw_title = _first_group(rules.patterns("title_page", "title"), markup)
    if raw_title:
        name = html_module.unescape(raw_title).strip()
        for pattern in rules.regex_list("title_page", "title_strip"):
            name = pattern.sub("", name).strip()
        # A generic landing page keeps only the site name - that is not a game.
        if name and not re.match(r"^prospero\s*patches", name, re.I):
            page.name = name

    page.data_key = _first_group(rules.patterns("title_page", "data_key"), markup)

    description = _first_group(rules.patterns("title_page", "description"), markup)
    if description:
        page.description = strip_tags(description)

    icon = _first_group(rules.patterns("title_page", "icon"), markup)
    if icon:
        page.icon_url = html_module.unescape(icon)
    banner = _first_group(rules.patterns("title_page", "banner"), markup)
    if banner:
        page.banner_url = html_module.unescape(banner)

    sidebar = rules.value("title_page", "sidebar") or {}
    block_pattern = rules.pattern("title_page", "sidebar") if isinstance(sidebar, str) else None
    if isinstance(sidebar, dict) and sidebar.get("block"):
        try:
            block_pattern = re.compile(str(sidebar["block"]))
            heading_pattern = re.compile(str(sidebar.get("heading", r"<strong[^>]*>([\s\S]*?)</strong>")))
        except re.error as exc:
            log.error("invalid sidebar regex: %s", exc)
            block_pattern = None
            heading_pattern = None
        mapping = {str(k).strip().lower(): str(v) for k, v in (sidebar.get("fields") or {}).items()}
        if block_pattern and heading_pattern:
            for block_match in block_pattern.finditer(markup):
                block = block_match.group(1)
                heading_match = heading_pattern.search(block)
                if not heading_match:
                    continue
                heading = strip_tags(heading_match.group(1))
                heading = re.sub(r"\b(View|Switch)\b", "", heading).strip()
                remainder = block[heading_match.end():]
                # keep anchor text (e.g. publisher names), drop "View" links
                remainder = re.sub(r"<a\b[^>]*>\s*View\s*</a>", " ", remainder, flags=re.I)
                remainder = re.sub(r"<a\b[^>]*dynamicmodal[^>]*>[\s\S]*?</a>", " ", remainder, flags=re.I)
                value = strip_tags(remainder)
                attribute = mapping.get(heading.lower())
                if attribute and value and not getattr(page, attribute, ""):
                    setattr(page, attribute, value)
    return page


def parse_patch_list(payload: Any, rules: Rules) -> Tuple[List[Patch], str]:
    """Convert a ``loadpatches`` style response into Patch objects."""
    if not isinstance(payload, dict):
        return [], ""
    success_key = rules.value("patches", "success_key", "success")
    if success_key and success_key in payload and not payload.get(success_key):
        return [], ""
    items = payload.get(rules.value("patches", "list_key", "patches"))
    if not isinstance(items, list):
        return [], ""

    fields = rules.fields("patches")
    patches: List[Patch] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content_ver = normalize_content_version(_pick(item, fields.get("content_ver", ["content_ver"])))
        if not content_ver:
            continue
        patches.append(
            Patch(
                content_ver=content_ver,
                file_size=parse_size(_pick(item, fields.get("file_size", ["filesize"]))),
                required_firmware=_clean_firmware(_pick(item, fields.get("required_firmware", ["required_firmware"]))),
                import_date=_as_text(_pick(item, fields.get("import_date", ["import_date"]))),
                is_latest=bool(_pick(item, fields.get("is_latest", ["is_latest"]))),
                changelog=strip_tags(_as_text(_pick(item, fields.get("changelog", ["changelog_preview"]))) or ""),
                keyset_patch=_as_text(_pick(item, fields.get("keyset_patch", ["keyset.patch"]))) or "",
                keyset_details=_as_text(_pick(item, fields.get("keyset_details", ["keyset.details"]))) or "",
                keyset_changeinfo=_as_text(_pick(item, fields.get("keyset_changeinfo", ["keyset.changeinfo"]))) or "",
            )
        )
    last_updated = _as_text(payload.get(rules.value("patches", "last_updated_key", "lastupdated"))) or ""
    return patches, last_updated


def parse_additional_content(payload: Any, rules: Rules) -> List[AdditionalContent]:
    if not isinstance(payload, dict):
        return []
    success_key = rules.value("additional_content", "success_key", "success")
    if success_key and success_key in payload and not payload.get(success_key):
        return []
    items = payload.get(rules.value("additional_content", "list_key", "items"))
    if not isinstance(items, list):
        return []
    fields = rules.fields("additional_content")
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(
            AdditionalContent(
                name=strip_tags(_as_text(_pick(item, fields.get("name", ["name"]))) or ""),
                content_id=_as_text(_pick(item, fields.get("content_id", ["contentid"]))) or "",
                content_ver=normalize_content_version(_pick(item, fields.get("content_ver", ["content_ver"]))),
                file_size=parse_size(_pick(item, fields.get("file_size", ["filesize"]))),
                required_firmware=_clean_firmware(_pick(item, fields.get("required_firmware", ["required_firmware"]))),
                icon_url=_as_text(_pick(item, fields.get("icon", ["icon"]))) or "",
                key=_as_text(_pick(item, fields.get("key", ["key"]))) or "",
            )
        )
    return result


def parse_title_links(text: str, rules: Rules, section: str) -> List[SearchResult]:
    """Extract ``/PPSAxxxxx`` links plus their label from HTML."""
    pattern = rules.pattern(section)
    if pattern is None:
        return []
    results: List[SearchResult] = []
    seen = set()
    for match in pattern.finditer(text or ""):
        groups = match.groups()
        title_id = (groups[0] if groups else "").upper()
        if not is_title_id(title_id) or title_id in seen:
            continue
        label = strip_tags(groups[1]) if len(groups) > 1 else ""
        seen.add(title_id)
        results.append(SearchResult(title_id=title_id, name=label))
    return results


def parse_search_payload(payload: Any, rules: Rules) -> List[SearchResult]:
    """Read search results out of a JSON response of unknown shape."""
    fields = rules.fields("search", "json_fields")
    if not fields:
        return []
    candidates: List[Dict[str, Any]] = []

    def collect(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for entry in node:
                collect(entry, depth + 1)
        elif isinstance(node, dict):
            if _pick(node, fields.get("title_id", ["titleid"])):
                candidates.append(node)
                return
            for value in node.values():
                collect(value, depth + 1)

    collect(payload)
    results: List[SearchResult] = []
    seen = set()
    for item in candidates:
        title_id = str(_pick(item, fields.get("title_id", ["titleid"])) or "").upper()
        if not is_title_id(title_id) or title_id in seen:
            continue
        seen.add(title_id)
        results.append(
            SearchResult(
                title_id=title_id,
                name=strip_tags(_as_text(_pick(item, fields.get("name", ["name"]))) or ""),
                region=_as_text(_pick(item, fields.get("region", ["region"]))) or "",
                icon_url=_as_text(_pick(item, fields.get("icon", ["icon"]))) or "",
            )
        )
    return results


def _as_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _clean_firmware(value: Any) -> Optional[str]:
    text = _as_text(value)
    if not text:
        return None
    match = re.search(r"\d{1,2}[.,]\d{2}", text)
    if match:
        return match.group(0).replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)*", text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class ProsperoClient:
    """Executes the rule-driven requests against PROSPEROPatches."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient, rules: Rules):
        self.settings = settings
        self.client = client
        self.rules = rules
        self.base_url = settings.prospero_base_url.rstrip("/")

    def set_rules(self, rules: Rules) -> None:
        self.rules = rules

    # -- request plumbing ---------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = dict(self.rules.headers)
        headers.setdefault("Referer", self.base_url + "/")
        headers.setdefault("Origin", self.base_url)
        return headers

    @staticmethod
    def _fill(template: str, context: Dict[str, str]) -> str:
        out = template
        for key, value in context.items():
            out = out.replace("{" + key + "}", value)
        return out

    @staticmethod
    def _has_unresolved(text: str) -> bool:
        return bool(re.search(r"\{[a-z_]+\}", text))

    async def _execute(self, rule: RequestRule, context: Dict[str, str]) -> Optional[httpx.Response]:
        path = self._fill(rule.path, context)
        params = {k: self._fill(v, context) for k, v in rule.params.items()}
        if self._has_unresolved(path) or any(self._has_unresolved(v) for v in params.values()):
            log.debug("skipping rule %s %s: unresolved placeholder", rule.method, rule.path)
            return None
        if any(not v for v in params.values()):
            log.debug("skipping rule %s %s: empty parameter", rule.method, rule.path)
            return None

        url = self.base_url + (path if path.startswith("/") else "/" + path)
        headers = self._headers()
        kwargs: Dict[str, Any] = {"headers": headers}
        if rule.method == "GET" or rule.encoding == "query":
            kwargs["params"] = params
        elif rule.encoding == "form":
            kwargs["data"] = params
        elif rule.encoding == "json":
            kwargs["json"] = params
        else:  # json-body-form-header: what the site's own frontend sends
            kwargs["content"] = json.dumps(params)
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        async def attempt(_: int) -> httpx.Response:
            response = await self.client.request(rule.method, url, **kwargs)
            raise_for_retryable(response, url)
            return response

        try:
            return await with_retries(
                attempt,
                settings=self.settings,
                description=f"PROSPERO {rule.method} {path}",
                max_retries=min(self.settings.http_max_retries, 2),
            )
        except Exception as exc:
            log.debug("PROSPERO request %s %s failed: %s", rule.method, path, exc)
            return None

    @staticmethod
    def _json_of(response: httpx.Response) -> Optional[Any]:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return None

    # -- public API ---------------------------------------------------------
    async def fetch_title_page(self, title_id: str) -> TitlePage:
        path = str(self.rules.value("title_page", "path", "/{title_id}"))
        rule = RequestRule(method="GET", path=path, params={}, encoding="query")
        response = await self._execute(rule, {"title_id": title_id})
        if response is None:
            raise ProsperoError(f"could not load the PROSPEROPatches page for {title_id}")
        if response.status_code == 404:
            raise ProsperoError(f"title {title_id} is not listed on PROSPEROPatches")
        if response.status_code >= 400:
            raise ProsperoError(f"PROSPEROPatches returned HTTP {response.status_code} for {title_id}")
        return parse_title_page(response.text, title_id, self.rules)

    async def fetch_patches(self, title_id: str, data_key: Optional[str]) -> Tuple[List[Patch], str]:
        context = {"title_id": title_id, "data_key": data_key or ""}
        for rule in self.rules.requests("patches"):
            response = await self._execute(rule, context)
            if response is None or response.status_code >= 400:
                continue
            payload = self._json_of(response)
            patches, last_updated = parse_patch_list(payload, self.rules)
            if patches:
                return patches, last_updated
        return [], ""

    async def fetch_additional_content(self, title_id: str, data_key: Optional[str]) -> List[AdditionalContent]:
        context = {"title_id": title_id, "data_key": data_key or ""}
        for rule in self.rules.requests("additional_content"):
            response = await self._execute(rule, context)
            if response is None or response.status_code >= 400:
                continue
            items = parse_additional_content(self._json_of(response), self.rules)
            if items:
                return items
        return []

    async def fetch_regions(self, title_id: str) -> List[SearchResult]:
        for rule in self.rules.requests("regions"):
            response = await self._execute(rule, {"title_id": title_id})
            if response is None or response.status_code >= 400:
                continue
            results = parse_title_links(response.text, self.rules, "regions")
            if results:
                return [r for r in results if r.title_id != title_id]
        return []

    async def search(self, query: str) -> List[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []
        for rule in self.rules.requests("search"):
            response = await self._execute(rule, {"query": query})
            if response is None or response.status_code >= 400:
                continue
            payload = self._json_of(response)
            results = parse_search_payload(payload, self.rules) if payload is not None else []
            if not results:
                text = response.text
                if payload is not None:
                    text = json.dumps(payload)
                results = parse_title_links(text, self.rules, "search")
            if results:
                return results
        return []

    async def fetch_title(self, title_id: str) -> TitleDetails:
        page = await self.fetch_title_page(title_id)
        details = TitleDetails(page=page)
        try:
            details.patches, details.last_updated = await self.fetch_patches(title_id, page.data_key)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("patch list for %s failed: %s", title_id, exc)
        try:
            details.additional_content = await self.fetch_additional_content(title_id, page.data_key)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("DLC list for %s failed: %s", title_id, exc)
        try:
            details.regions = await self.fetch_regions(title_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("region list for %s failed: %s", title_id, exc)
        return details

    async def discover_links(self, title_id: str, patch: Optional[Patch]) -> List[str]:
        """Scan the configured detail endpoints for official Sony links."""
        context = {
            "title_id": title_id,
            "content_ver": patch.content_ver if patch else "",
            "keyset_patch": patch.keyset_patch if patch else "",
            "keyset_details": patch.keyset_details if patch else "",
            "keyset_changeinfo": patch.keyset_changeinfo if patch else "",
            "content_id": "",
        }
        found: List[str] = []
        for rule in self.rules.requests("link_resolution"):
            response = await self._execute(rule, context)
            if response is None or response.status_code >= 400:
                continue
            for url in sony.extract_playstation_links(response.text):
                if url not in found:
                    found.append(url)
            if found:
                break
        return found
