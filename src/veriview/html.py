"""Interactive, self-contained HTML viewer with hierarchy navigation.

For every requested view stage (elab / opt / gate) each module of the
hierarchical netlist is rendered to SVG -- plus a flattened variant for modules
that contain sub-modules -- and embedded together with a slim connectivity
netlist into a single HTML file. In the browser the user can switch stages,
toggle flattening, navigate into sub-modules and highlight logic cones, all
without a server or network access.
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
from .scope import TOP_ATTR, find_top, postprocess, reroot, user_children

TEMPLATE = Path(__file__).with_name("viewer.html")
SIM_JS = Path(__file__).with_name("sim.js")
SIM_PLACEHOLDER = "/*__VERIVIEW_SIM__*/"
TIMING_JS = Path(__file__).with_name("timing.js")
TIMING_PLACEHOLDER = "/*__VERIVIEW_TIMING__*/"
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
    # older Yosys versions store hdlname with a leading backslash
    base = ((attrs or {}).get("hdlname") or m.group(1)).lstrip("\\")
    params = []
    for p in (m.group(2) or "").split("\\"):
        if p:
            k, _, v = p.partition("=")
            params.append(f"{k}={decode_param(v)}")
    return f"{base} ({', '.join(params)})" if params else base


def slim_netlist(mod: dict[str, Any]) -> dict[str, Any]:
    """Connectivity (+ cell parameters) for cone highlighting and simulation in the browser."""
    cells = {}
    for name, cell in mod.get("cells", {}).items():
        dirs = cell.get("port_directions", {})
        ctype = cell.get("type", "")
        entry = [ctype, {p: [dirs.get(p, ""), bits] for p, bits in cell.get("connections", {}).items()}]
        if ctype.startswith("$") and cell.get("parameters"):
            # widths, signedness, polarities, reset values: needed by the simulator
            entry.append(cell["parameters"])
        cells[name] = entry
    ports = {name: [p.get("direction", ""), p.get("bits", [])] for name, p in mod.get("ports", {}).items()}
    names = {n: v["bits"] for n, v in mod.get("netnames", {}).items()
             if not v.get("hide_name") and not n.startswith("$")}
    return {"cells": cells, "ports": ports, "names": names, **source_locations(mod)}


def source_locations(mod: dict[str, Any]) -> dict[str, Any]:
    """Source locations ("file:line.col-line.col", '|'-separated) of cells and nets.

    Gates produced by technology mapping (abc) carry no location; they inherit the
    location of the net they drive (public names preferred), which in practice points
    at the RTL line the logic came from.
    """
    bit_src: dict[int, str] = {}
    nsrc: dict[str, str] = {}
    for n, v in sorted(mod.get("netnames", {}).items(), key=lambda kv: (kv[1].get("hide_name", 0), len(kv[0]))):
        src = v.get("attributes", {}).get("src")
        if not src:
            continue
        if not v.get("hide_name") and not n.startswith("$"):
            nsrc[n] = src
        for b in v.get("bits", []):
            if isinstance(b, int):
                bit_src.setdefault(b, src)
    csrc: dict[str, str] = {}
    for name, cell in mod.get("cells", {}).items():
        src = cell.get("attributes", {}).get("src")
        if not src:
            dirs = cell.get("port_directions", {})
            for p, bits in cell.get("connections", {}).items():
                if dirs.get(p) == "output":
                    src = next((bit_src[b] for b in bits if isinstance(b, int) and b in bit_src), None)
                    if src:
                        break
        if src:
            csrc[name] = src
    return {"src": csrc, "nsrc": nsrc}


MAX_SOURCE_BYTES = 2_000_000


def collect_sources(stage_netlists: dict[str, dict[str, Any]], search_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    """Read every source file referenced by a src attribute: {path-as-in-src: {text, note}}."""
    paths: set[str] = set()

    def add(src: str | None) -> None:
        for part in (src or "").split("|"):
            if ":" in part:
                paths.add(part.rsplit(":", 1)[0])

    for net in stage_netlists.values():
        for mod in net["modules"].values():
            add(mod.get("attributes", {}).get("src"))
            for c in mod.get("cells", {}).values():
                add(c.get("attributes", {}).get("src"))
            for v in mod.get("netnames", {}).values():
                add(v.get("attributes", {}).get("src"))
    out: dict[str, dict[str, Any]] = {}
    total = 0
    for path in sorted(paths):
        for base in search_dirs:
            f = Path(path) if Path(path).is_absolute() else base / path
            if f.is_file():
                text = f.read_text(encoding="utf-8", errors="replace")
                if total + len(text) > MAX_SOURCE_BYTES:
                    log.warn(f"Source not embedded in the viewer (size limit): {path}")
                    break
                total += len(text)
                note = "converted to Verilog by sv2v" if path.endswith(".sv2v.v") else ""
                out[path] = {"text": text, "note": note}
                break
    return out


def _single_module(netlist: dict[str, Any], name: str, mod: dict[str, Any]) -> dict[str, Any]:
    mod = dict(mod)
    mod["attributes"] = {**mod.get("attributes", {}), "top": TOP_ATTR}
    return {"creator": netlist.get("creator", ""), "modules": {name: mod}}


def strip_xml_decl(svg: str) -> str:
    return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", svg)


def build_viewer(stage_netlists: dict[str, dict[str, Any]], out_html: Path, work_dir: Path, *,
                 yosys: str, skin: Path | None = None, title: str | None = None,
                 default_stage: str | None = None, default_flat: bool = False,
                 fsms: dict[str, Any] | None = None, lang: str = "auto") -> None:
    """Write the viewer. `stage_netlists` maps stage name -> hierarchical Yosys JSON netlist."""
    if not stage_netlists:
        raise ValueError("no netlists")
    stages = list(stage_netlists)

    # Jobs: (stage, module, flat?) -> (svg, slim netlist)
    jobs: list[tuple[str, str, bool]] = []
    for st, net in stage_netlists.items():
        for name in net["modules"]:
            jobs.append((st, name, False))
            if user_children(net["modules"], name):
                jobs.append((st, name, True))

    def run(idx: int) -> tuple[str, dict[str, Any]]:
        st, name, flat = jobs[idx]
        net = stage_netlists[st]
        mod = net["modules"][name]
        if flat:
            sub, root = reroot(net, name)
            sub = postprocess(yosys, work_dir, sub, root, flatten=True, cones=[], tag=f"flat{idx}")
            single = _single_module(sub, root, sub["modules"][root])
            mod = sub["modules"][root]
        else:
            single = _single_module(net, name, mod)
        jpath = work_dir / f"view{idx}.json"
        jpath.write_text(json.dumps(single))
        svg = run_netlistsvg(jpath, work_dir / f"view{idx}.svg", skin)
        return strip_xml_decl(svg), slim_netlist(mod)

    log.info(f"Rendering {len(jobs)} view(s) for stage(s): {', '.join(stages)}...")
    workers = max(1, min(8, os.cpu_count() or 1, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, range(len(jobs))))

    views: dict[str, Any] = {}
    for (st, name, flat), (svg, slim) in zip(jobs, results):
        net = stage_netlists[st]
        mod = net["modules"][name]
        view = views.setdefault(st, {"top": find_top(net["modules"]), "modules": {}})
        entry = view["modules"].setdefault(name, {
            "label": display_name(name, mod.get("attributes")),
            "src": mod.get("attributes", {}).get("src", ""),
            "children": user_children(net["modules"], name),
        })
        entry["flat" if flat else "hier"] = {"svg": svg, "net": slim, "cells": len(slim["cells"])}

    first = views[stages[0]]
    data = {
        "version": __version__,
        "stages": stages,
        "defaultStage": default_stage if default_stage in views else stages[0],
        "defaultFlat": default_flat,
        "sources": collect_sources(stage_netlists, [Path.cwd(), work_dir]),
        "fsms": fsms or {},
        "lang": lang,
        "views": views,
    }
    # '<' only occurs inside JSON strings, so escaping it keeps </script> and
    # <!-- in SVG text from ending/confusing the inline <script>.
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    page = TEMPLATE.read_text(encoding="utf-8")
    top_label = first["modules"][first["top"]]["label"]
    page = page.replace(TITLE_PLACEHOLDER, _html_escape(title or top_label))
    page = page.replace(SIM_PLACEHOLDER, SIM_JS.read_text(encoding="utf-8").replace("</", "<\\/"), 1)
    page = page.replace(TIMING_PLACEHOLDER, TIMING_JS.read_text(encoding="utf-8").replace("</", "<\\/"), 1)
    page = page.replace(DATA_PLACEHOLDER, payload)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page, encoding="utf-8")
    log.info(f"Viewer saved: {out_html}")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
