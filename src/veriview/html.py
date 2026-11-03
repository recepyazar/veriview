"""Interactive, self-contained HTML viewer with hierarchy navigation.

Every module of a hierarchical Yosys netlist is rendered to its own SVG with
netlistsvg; the SVGs plus the instance -> module map are embedded into a single
HTML file (no network access needed) where clicking a sub-module instance opens
that module's schematic.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import __version__, log
from .render import run_netlistsvg

TEMPLATE = Path(__file__).with_name("viewer.html")
DATA_PLACEHOLDER = "/*__VERIVIEW_DATA__*/null"
TITLE_PLACEHOLDER = "__VERIVIEW_TITLE__"

# $paramod\name\P1=val\P2=val  or, for long names, $paramod$<hash>\name\...
_PARAMOD_RE = re.compile(r"^\$paramod(?:\$[0-9a-fA-F]+)?\\([^\\]+)(?:\\(.*))?$", re.S)
_CONST_RE = re.compile(r"^(s?)(\d+)'([01]+)$")


def decode_param(value: str) -> str:
    """Decode a Yosys constant like s32'00..011 into a readable number."""
    m = _CONST_RE.match(value)
    if not m:
        return value
    signed, bits = m.group(1) == "s", m.group(3)
    n = int(bits, 2)
    if signed and bits[0] == "1":
        n -= 1 << len(bits)
    return str(n)


def display_name(module: str, attrs: dict[str, Any] | None = None) -> str:
    """'$paramod\\b2g\\WIDTH=s32'..011' -> 'b2g (WIDTH=3)'."""
    m = _PARAMOD_RE.match(module)
    if not m:
        return module
    base = (attrs or {}).get("hdlname") or m.group(1)
    params = []
    for p in (m.group(2) or "").split("\\"):
        if p:
            k, _, v = p.partition("=")
            params.append(f"{k}={decode_param(v)}")
    return f"{base} ({', '.join(params)})" if params else base


def _is_set(value: Any) -> bool:
    if isinstance(value, str):
        try:
            return int(value, 2) != 0
        except ValueError:
            return value not in ("", "0")
    return bool(value)


def find_top(modules: dict[str, Any]) -> str:
    for name, mod in modules.items():
        if _is_set(mod.get("attributes", {}).get("top", 0)):
            return name
    return next(iter(modules))


def build_viewer(netlist: dict[str, Any], out_html: Path, work_dir: Path,
                 skin: Path | None = None, title: str | None = None) -> None:
    modules: dict[str, Any] = netlist.get("modules", {})
    if not modules:
        raise ValueError("netlist contains no modules")
    top = find_top(modules)
    names = list(modules)

    def render_one(idx: int) -> str:
        name = names[idx]
        mod = dict(modules[name])
        mod["attributes"] = {**mod.get("attributes", {}), "top": "00000000000000000000000000000001"}
        single = {"creator": netlist.get("creator", ""), "modules": {name: mod}}
        jpath = work_dir / f"html_m{idx}.json"
        jpath.write_text(json.dumps(single))
        return run_netlistsvg(jpath, work_dir / f"html_m{idx}.svg", skin)

    log.info(f"Rendering {len(names)} module(s)...")
    workers = max(1, min(8, os.cpu_count() or 1, len(names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        svgs = list(pool.map(render_one, range(len(names))))

    data = {
        "version": __version__,
        "top": top,
        "modules": {
            name: {
                "label": display_name(name, mod.get("attributes")),
                "src": mod.get("attributes", {}).get("src", ""),
                "cells": len(mod.get("cells", {})),
                "ports": len(mod.get("ports", {})),
                # instance name -> module name, only for user modules (navigable)
                "children": {
                    cell: info["type"] for cell, info in mod.get("cells", {}).items() if info.get("type") in modules
                },
                "svg": re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", svg),
            }
            for name, mod, svg in zip(names, modules.values(), svgs)
        },
    }
    # '<' only occurs inside JSON strings, so escaping it keeps </script> and
    # <!-- in SVG text from ending/confusing the inline <script>.
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace(TITLE_PLACEHOLDER, _html_escape(title or display_name(top)))
    page = page.replace(DATA_PLACEHOLDER, payload)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page, encoding="utf-8")
    log.info(f"Viewer saved: {out_html}")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
