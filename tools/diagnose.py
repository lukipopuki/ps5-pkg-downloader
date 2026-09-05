#!/usr/bin/env python3
"""Collect what the PROSPEROPatches provider actually sees.

The provider is driven by regular expressions and request paths that can only
be repaired against real responses.  This script performs the same requests the
application performs, stores every raw response, and writes a summary of what
matched and what did not.  Hand the resulting archive to whoever adapts
``prospero_rules.yaml``.

It deliberately uses nothing but the Python standard library, so it runs on
macOS, on Unraid and inside the container without installing anything:

    python3 diagnose.py PPSA08338
    python3 diagnose.py PPSA08338 --search "spider-man" --app http://localhost:8080

Nothing secret is collected: only public pages, their status codes and the
application's own API responses.  Pass --app only for an instance you own.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE = "https://prosperopatches.com"
TIMEOUT = 25

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# Mirrors the shipped prospero_rules.yaml. Kept inline so the script stays
# dependency free; the point is to find out which of these still work.
PROBES: List[Dict[str, Any]] = [
    {"name": "title-page", "method": "GET", "path": "/{title_id}", "params": {}, "encoding": "query"},
    {"name": "loadpatches-jsonbody", "method": "POST", "path": "/api/internal/loadpatches",
     "params": {"titleid": "{title_id}", "key": "{data_key}"}, "encoding": "json-body-form-header"},
    {"name": "loadpatches-form", "method": "POST", "path": "/api/internal/loadpatches",
     "params": {"titleid": "{title_id}", "key": "{data_key}"}, "encoding": "form"},
    {"name": "loadpatches-json", "method": "POST", "path": "/api/internal/loadpatches",
     "params": {"titleid": "{title_id}", "key": "{data_key}"}, "encoding": "json"},
    {"name": "loadac", "method": "POST", "path": "/api/internal/loadac",
     "params": {"titleid": "{title_id}", "key": "{data_key}"}, "encoding": "json-body-form-header"},
    {"name": "switch-region", "method": "GET", "path": "/api/internal/data/switch-region.php",
     "params": {"titleid": "{title_id}"}, "encoding": "query"},
    # Search: none of these is confirmed, that is what we are here to find out.
    {"name": "search-api", "method": "POST", "path": "/api/internal/search",
     "params": {"query": "{query}"}, "encoding": "json-body-form-header", "needs": "query"},
    {"name": "search-loadsearch", "method": "POST", "path": "/api/internal/loadsearch",
     "params": {"query": "{query}"}, "encoding": "json-body-form-header", "needs": "query"},
    {"name": "search-php", "method": "GET", "path": "/api/internal/data/search.php",
     "params": {"q": "{query}"}, "encoding": "query", "needs": "query"},
    {"name": "search-page", "method": "GET", "path": "/search",
     "params": {"q": "{query}"}, "encoding": "query", "needs": "query"},
    {"name": "search-root", "method": "GET", "path": "/",
     "params": {"s": "{query}"}, "encoding": "query", "needs": "query"},
    # Download link resolution: the modal endpoints behind the keyset values.
    {"name": "loadpatchdetails", "method": "POST", "path": "/api/internal/loadpatchdetails",
     "params": {"titleid": "{title_id}", "key": "{keyset_details}"},
     "encoding": "json-body-form-header", "needs": "keyset_details"},
    {"name": "loaddetails", "method": "POST", "path": "/api/internal/loaddetails",
     "params": {"titleid": "{title_id}", "key": "{keyset_details}"},
     "encoding": "json-body-form-header", "needs": "keyset_details"},
    {"name": "loadpatch", "method": "POST", "path": "/api/internal/loadpatch",
     "params": {"titleid": "{title_id}", "key": "{keyset_patch}"},
     "encoding": "json-body-form-header", "needs": "keyset_patch"},
    {"name": "loadchangeinfo", "method": "POST", "path": "/api/internal/loadchangeinfo",
     "params": {"titleid": "{title_id}", "key": "{keyset_changeinfo}"},
     "encoding": "json-body-form-header", "needs": "keyset_changeinfo"},
]

DATA_KEY_PATTERNS = [
    r'id="dynpatch"[^>]*data-key="([a-f0-9]+)"',
    r'data-key="([a-f0-9]+)"[^>]*id="dynpatch"',
    r'data-key="([a-f0-9]+)"',
]

PLAYSTATION_LINK_RE = re.compile(r"https?://[A-Za-z0-9.\-]*playstation\.net/[^\s\"'<>\\)]+", re.I)
TITLE_LINK_RE = re.compile(r'href=\\?"/([A-Z]{4}[0-9]{5})\\?"')

# Endpoint discovery: the site's own markup and scripts name the paths it
# calls. Finding them beats guessing them.
ENDPOINT_RE = re.compile(r"""["'`](/(?:api|ajax|internal)/[A-Za-z0-9_./-]{2,90}|/[A-Za-z0-9_./-]{2,90}\.php)["'`]""")
SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
DATA_ATTR_RE = re.compile(r'\b(data-[a-z0-9-]+)="([^"]{0,80})"')
MAX_SCRIPTS = 12
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_DISCOVERED_PROBES = 20


def fill(template: str, context: Dict[str, str]) -> str:
    out = template
    for key, value in context.items():
        out = out.replace("{" + key + "}", value)
    return out


def request(
    method: str,
    url: str,
    params: Dict[str, str],
    encoding: str,
    base: str,
) -> Tuple[Optional[int], Dict[str, str], bytes, str]:
    headers = dict(BROWSER_HEADERS)
    headers["Referer"] = base + "/"
    headers["Origin"] = base
    body = None

    if method == "GET" or encoding == "query":
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
    elif encoding == "form":
        body = urllib.parse.urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    elif encoding == "json":
        body = json.dumps(params).encode()
        headers["Content-Type"] = "application/json"
    else:  # json-body-form-header: what the site's own frontend sends
        body = json.dumps(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=context) as response:
            return response.status, dict(response.headers), response.read(), ""
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b"", ""
    except Exception as exc:  # network, DNS, TLS
        return None, {}, b"", f"{type(exc).__name__}: {exc}"


def describe(body: bytes) -> Dict[str, Any]:
    """What can be learned from a response without knowing its shape."""
    text = body.decode("utf-8", errors="replace")
    info: Dict[str, Any] = {"bytes": len(body)}
    try:
        payload = json.loads(text)
        info["json"] = True
        if isinstance(payload, dict):
            info["json_keys"] = sorted(payload.keys())[:20]
            for key, value in payload.items():
                if isinstance(value, list) and value:
                    info["list_key"] = key
                    info["list_length"] = len(value)
                    if isinstance(value[0], dict):
                        info["item_keys"] = sorted(value[0].keys())[:25]
                    break
    except ValueError:
        info["json"] = False
        title = re.search(r"<title>([^<]*)</title>", text, re.I)
        if title:
            info["html_title"] = title.group(1).strip()[:120]

    links = []
    for match in PLAYSTATION_LINK_RE.finditer(text):
        url = match.group(0).replace("\\/", "/").rstrip(".,;\\")
        if url not in links:
            links.append(url)
    if links:
        info["playstation_links"] = links[:15]

    titles = sorted(set(TITLE_LINK_RE.findall(text)))
    if titles:
        info["title_links"] = titles[:25]
    return info


def probe_app(app_url: str, title_id: str, query: str, out_dir: Path, summary: List[str]) -> None:
    """Capture what the running application itself answers."""
    endpoints = [
        ("health", f"{app_url}/api/health"),
        ("settings", f"{app_url}/api/settings"),
        ("search", f"{app_url}/api/search?q={urllib.parse.quote(query or title_id)}"),
        ("title", f"{app_url}/api/title/{title_id}"),
        ("downloads", f"{app_url}/api/downloads"),
    ]
    summary.append("")
    summary.append("=== application API ===")
    for name, url in endpoints:
        status, _, body, error = request("GET", url, {}, "query", app_url)
        path = out_dir / f"app-{name}.json"
        path.write_bytes(body)
        if error:
            summary.append(f"  {name:10s} FAILED  {error}")
            continue
        note = ""
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and "detail" in payload:
                note = f"  detail={payload['detail'][:120]}"
            elif name == "search" and isinstance(payload, dict):
                note = f"  results={len(payload.get('results') or [])}"
            elif name == "title" and isinstance(payload, dict):
                note = f"  updates={len(payload.get('updates') or [])}"
        except ValueError:
            pass
        summary.append(f"  {name:10s} HTTP {status}  {len(body)} bytes{note}")


def discover_and_probe(
    base: str,
    out_dir: Path,
    context: Dict[str, str],
    summary: List[str],
    findings: Dict[str, Any],
) -> None:
    """Read the site's own markup and scripts, then try what they reference.

    None of the configured endpoints produced an official link, so the paths
    have to come from somewhere real: the title page and the JavaScript it
    loads both spell out the URLs the site calls.
    """
    summary.append("")
    summary.append("=== endpoint discovery ===")

    page = out_dir / "title-page.html"
    if not page.exists():
        summary.append("  no title page captured, nothing to inspect")
        return
    markup = page.read_text(encoding="utf-8", errors="replace")

    sources: List[Tuple[str, str]] = [("title-page", markup)]

    scripts = [s for s in SCRIPT_SRC_RE.findall(markup) if not s.startswith(("http://", "https://"))
               or urllib.parse.urlparse(s).netloc.endswith(urllib.parse.urlparse(base).netloc)
               or "prospero" in s]
    for index, src in enumerate(scripts[:MAX_SCRIPTS]):
        url = src if src.startswith("http") else base + ("" if src.startswith("/") else "/") + src
        status, _, body, error = request("GET", url, {}, "query", base)
        if error or not body or len(body) > MAX_SCRIPT_BYTES:
            summary.append(f"  script {src[:70]:70s} {error or f'HTTP {status}'}")
            continue
        name = f"script-{index:02d}-" + re.sub(r"[^A-Za-z0-9._-]", "_", src.split("/")[-1])[:60]
        (out_dir / name).write_bytes(body)
        sources.append((name, body.decode("utf-8", errors="replace")))
        summary.append(f"  script {src[:70]:70s} HTTP {status} {len(body)} bytes")

    # data-* attributes carry the keys the frontend passes to those endpoints.
    attributes: Dict[str, str] = {}
    for attr, value in DATA_ATTR_RE.findall(markup):
        attributes.setdefault(attr, value)
    if attributes:
        summary.append("  data attributes on the page:")
        for attr, value in sorted(attributes.items())[:25]:
            summary.append(f"      {attr}=\"{value[:60]}\"")

    discovered: Dict[str, str] = {}
    for origin, text in sources:
        for path in ENDPOINT_RE.findall(text):
            discovered.setdefault(path, origin)
    if not discovered:
        summary.append("  no endpoint paths found in the page or its scripts")
        return

    summary.append(f"  endpoint paths referenced by the site ({len(discovered)}):")
    for path, origin in sorted(discovered.items()):
        summary.append(f"      {path:60s} (from {origin})")

    already = {"/api/internal/loadpatches", "/api/internal/loadac",
               "/api/internal/data/switch-region.php", "/api/internal/loadpatchdetails",
               "/api/internal/loaddetails", "/api/internal/loadpatch",
               "/api/internal/loadchangeinfo", "/api/internal/search"}
    candidates = [p for p in sorted(discovered) if p not in already]

    keys = [(label, context[label]) for label in
            ("keyset_details", "keyset_patch", "keyset_changeinfo", "data_key")
            if context.get(label)]
    if not keys:
        summary.append("  no keys available, cannot probe the discovered endpoints")
        return

    summary.append("")
    summary.append("=== probing discovered endpoints ===")
    probed = 0
    for path in candidates:
        if probed >= MAX_DISCOVERED_PROBES:
            summary.append(f"  (stopped after {MAX_DISCOVERED_PROBES} endpoints)")
            break
        for label, value in keys:
            probed += 1
            params = {"titleid": context["title_id"], "key": value}
            status, headers, body, error = request(
                "POST", base + path, params, "json-body-form-header", base
            )
            if error:
                summary.append(f"  POST {path:52s} [{label}] FAILED {error}")
                continue
            info = describe(body)
            note = ""
            if info.get("playstation_links"):
                note = f"  <<< OFFICIAL LINKS: {info['playstation_links'][:3]}"
                name = "discovered-" + re.sub(r"[^A-Za-z0-9]", "_", path)[:60] + f"-{label}"
                suffix = "json" if info.get("json") else "html"
                (out_dir / f"{name}.{suffix}").write_bytes(body)
                findings[f"discovered:{path}:{label}"] = {"status": status, **info}
            elif info.get("json"):
                note = f"  json keys={info.get('json_keys')}"
            summary.append(
                f"  POST {path:52s} [{label}] HTTP {status:<4} {info['bytes']:>7} bytes{note}"
            )
            if info.get("playstation_links"):
                break  # this key works, no need to try the others


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("title_id", help="a title to probe, e.g. PPSA08338")
    parser.add_argument("--search", default="", help="a game name to try the search endpoints with")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"index base URL (default {DEFAULT_BASE})")
    parser.add_argument("--app", default="", help="base URL of a running instance, e.g. http://localhost:8080")
    parser.add_argument("--out", default="", help="output directory (default: prospero-report-<timestamp>)")
    args = parser.parse_args()

    title_id = args.title_id.strip().upper()
    base = args.base.rstrip("/")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or f"prospero-report-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    context = {
        "title_id": title_id,
        "query": args.search.strip(),
        "data_key": "",
        "keyset_patch": "",
        "keyset_details": "",
        "keyset_changeinfo": "",
    }

    summary = [
        f"PROSPERO diagnostic report  {stamp}",
        f"base    : {base}",
        f"title id: {title_id}",
        f"search  : {context['query'] or '(none)'}",
        "",
        "=== index requests ===",
    ]
    findings: Dict[str, Any] = {}

    for probe in PROBES:
        needs = probe.get("needs")
        if needs and not context.get(needs):
            summary.append(f"  {probe['name']:24s} SKIPPED (no {needs})")
            continue

        url = base + fill(probe["path"], context)
        params = {k: fill(v, context) for k, v in probe["params"].items()}
        status, headers, body, error = request(probe["method"], url, params, probe["encoding"], base)

        if error:
            summary.append(f"  {probe['name']:24s} FAILED   {error}")
            continue

        suffix = "json" if "json" in headers.get("Content-Type", "") else "html"
        (out_dir / f"{probe['name']}.{suffix}").write_bytes(body)

        info = describe(body)
        findings[probe["name"]] = {"status": status, **info}
        detail = f"HTTP {status:<4} {info['bytes']:>8} bytes"
        if info.get("json"):
            detail += f"  json keys={info.get('json_keys')}"
            if "item_keys" in info:
                detail += f" list='{info.get('list_key')}'({info.get('list_length')}) item_keys={info['item_keys']}"
        elif info.get("html_title"):
            detail += f"  <title>{info['html_title']}"
        if info.get("playstation_links"):
            detail += f"  PLAYSTATION LINKS: {len(info['playstation_links'])}"
        if info.get("title_links"):
            detail += f"  title links: {len(info['title_links'])}"
        summary.append(f"  {probe['name']:24s} {detail}")

        # The title page unlocks everything else.
        if probe["name"] == "title-page":
            text = body.decode("utf-8", errors="replace")
            for pattern in DATA_KEY_PATTERNS:
                match = re.search(pattern, text)
                if match:
                    context["data_key"] = match.group(1)
                    summary.append(f"      data-key matched by: {pattern}")
                    break
            if not context["data_key"]:
                summary.append("      !! no data-key found - the patch endpoints cannot be probed")
                for hint in re.findall(r'data-[a-z-]+="[^"]{4,64}"', text)[:12]:
                    summary.append(f"      candidate attribute: {hint}")

        # Patch list unlocks the keyset values used by the detail endpoints.
        if probe["name"].startswith("loadpatches") and not context["keyset_patch"]:
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                items = payload.get("patches")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    keyset = items[0].get("keyset") or {}
                    if isinstance(keyset, dict):
                        context["keyset_patch"] = str(keyset.get("patch") or "")
                        context["keyset_details"] = str(keyset.get("details") or "")
                        context["keyset_changeinfo"] = str(keyset.get("changeinfo") or "")
                        summary.append(f"      keyset: {sorted(keyset.keys())}")

    # If nothing so far produced an official link, go looking for the endpoints
    # the site itself calls instead of relying on the guessed list.
    if not any(data.get("playstation_links") for data in findings.values()):
        discover_and_probe(base, out_dir, context, summary, findings)

    if args.app:
        probe_app(args.app.rstrip("/"), title_id, context["query"], out_dir, summary)

    summary.append("")
    summary.append("=== what to look at first ===")
    with_links = [name for name, data in findings.items() if data.get("playstation_links")]
    summary.append(
        f"  official links found in : {', '.join(with_links) if with_links else 'NONE - link resolution has no source'}"
    )
    with_titles = [name for name, data in findings.items() if data.get("title_links")]
    summary.append(f"  title links found in    : {', '.join(with_titles) if with_titles else 'none'}")
    summary.append(f"  data-key                : {context['data_key'] or 'NOT FOUND'}")

    (out_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    (out_dir / "findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")

    archive = Path(f"{out_dir.name}.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(out_dir, arcname=out_dir.name)

    print("\n".join(summary))
    print()
    print(f"Raw responses : {out_dir}/")
    print(f"Archive       : {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
