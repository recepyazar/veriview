"""Interactive, self-contained HTML viewer with hierarchy navigation.

For every requested view stage (elab / opt / gate) each module of the
hierarchical netlist is rendered to SVG -- plus a flattened variant for modules
that contain sub-modules -- and embedded together with a slim connectivity
netlist into a single HTML file. In the browser the user can switch stages,
toggle flattening, navigate into sub-modules and highlight logic cones, all
without a server or network access.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import __version__, log
from .log import VeriViewError
from .render import DrawTimeout, run_netlistsvg
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
    """'$paramod\\b2g\\WIDTH=s32'..011' -> 'b2g (WIDTH=3)'; slang's 'kz_alu$top.u_alu' -> 'kz_alu'."""
    m = _PARAMOD_RE.match(module)
    if not m:
        # yosys-slang (--keep-hierarchy) names each specialization <module>$<instance path>
        if "$" in module and not module.startswith("$"):
            return module.split("$", 1)[0]
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


# netlistsvg (ELK layout) gets very slow and eventually overflows its stack on huge schematics:
# above this many cells a view is not drawn (the hierarchy, its sub-modules, the simulation and
# the timing analysis still work).
MAX_DRAW_CELLS = 3000
# flattened variants of bigger modules mostly take minutes to lay out (their sub-modules are drawn anyway)
MAX_FLAT_CELLS = 400
# viewer data above this size is embedded gzip-compressed (VERIVIEW_GZIP_MIN, bytes)
GZIP_MIN = int(os.environ.get("VERIVIEW_GZIP_MIN", "8000000"))
# a schematic whose layout takes longer than this is skipped (VERIVIEW_DRAW_TIMEOUT, seconds)
DRAW_TIMEOUT = float(os.environ.get("VERIVIEW_DRAW_TIMEOUT", "120"))
# above this many cells the layout uses faster (slightly less tidy) ELK settings (render.FAST_LAYOUT)
FAST_LAYOUT_CELLS = 300


def flat_cells(modules: dict[str, Any], name: str, memo: dict[str, int] | None = None) -> int:
    """Number of cells of a module once flattened (sub-module instances replaced by their contents)."""
    memo = {} if memo is None else memo
    if name not in memo:
        memo[name] = 0   # recursion guard
        total = 0
        for c in modules[name].get("cells", {}).values():
            total += flat_cells(modules, c["type"], memo) if c.get("type") in modules else 1
        memo[name] = total
    return memo[name]


def placeholder_svg(kind: str, cells: int, detail: str = "") -> str:
    """Drawn instead of a schematic that is too big (or failed): the viewer translates the text."""
    msg = {"big": "Too large to draw: {n} cells.", "failed": "This schematic could not be drawn ({n} cells).",
           "slow": "Not drawn: laying out {n} cells took too long."}[kind]
    hint = "Open its sub-modules in the tree on the left, or choose a smaller top module."
    import html as _h
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="640" height="120" viewBox="0 0 640 120" '
            'class="vv-placeholder"><rect x="1" y="1" width="638" height="118" rx="10" fill="none" '
            'stroke="#999" stroke-dasharray="6 4"/>'
            f'<text x="320" y="52" text-anchor="middle" font-size="16" data-i18n="{_h.escape(msg)}" '
            f'data-n="{cells}">{_h.escape(msg.replace("{n}", str(cells)))}</text>'
            f'<text x="320" y="80" text-anchor="middle" font-size="13" fill="#888" '
            f'data-i18n="{_h.escape(hint)}">{_h.escape(hint)}</text>'
            f'<desc>{_h.escape(detail[:2000])}</desc></svg>')


def draw_view(jpath: Path, svg_path: Path, what: str, cells: int, skin: Path | None) -> str:
    """One schematic (or a placeholder if it fails or takes too long)."""
    t0 = time.monotonic()
    try:
        svg = run_netlistsvg(jpath, svg_path, skin, timeout=DRAW_TIMEOUT, fast=cells > FAST_LAYOUT_CELLS)
    except DrawTimeout:
        log.warn(f"Not drawn, layout took over {DRAW_TIMEOUT:.0f} s: {what}")
        return placeholder_svg("slow", cells)
    except VeriViewError as e:   # one schematic failing must not lose the whole viewer
        log.warn(f"Could not draw {what}: {str(e).splitlines()[-1][:200]}")
        return placeholder_svg("failed", cells, str(e))
    dt = time.monotonic() - t0
    if dt > 10:
        log.info(f"Drawing {what} took {dt:.0f} s ({cells} cells)")
    return strip_xml_decl(svg)


# Launcher builds of big designs (more views or cells than below): only each view's top is drawn up
# front; every other schematic (flattened ones too) is drawn by render_lazy() when first opened
LAZY_MIN_VIEWS = 40
LAZY_MIN_CELLS = 5000
_lazy_locks: dict[str, threading.Lock] = {}
_lazy_guard = threading.Lock()


def render_lazy(lazy_dir: Path, idx: int) -> dict[str, Any]:
    """Draw (once) a view left for later by build_viewer(lazy_dir=...): {"svg": ..., "net": slim netlist}."""
    done = lazy_dir / f"view{idx}.done.json"
    with _lazy_guard:
        lock = _lazy_locks.setdefault(f"{lazy_dir}:{idx}", threading.Lock())
    with lock:
        if done.is_file():
            return json.loads(done.read_text(encoding="utf-8"))
        meta = json.loads((lazy_dir / f"view{idx}.meta.json").read_text(encoding="utf-8"))
        skin = Path(meta["skin"]) if meta.get("skin") else None
        jpath = lazy_dir / f"view{idx}.json"
        out: dict[str, Any] = {}
        if meta.get("flat"):   # flattened view: flatten now (Yosys), then draw
            sub = json.loads((lazy_dir / f"view{idx}.sub.json").read_text(encoding="utf-8"))
            root = meta["root"]
            sub = postprocess(meta["yosys"], lazy_dir, sub, root, flatten=True, cones=[], tag=f"lazyflat{idx}")
            mod = sub["modules"][root]
            jpath.write_text(json.dumps(_single_module(sub, root, mod)))
            out["net"] = slim_netlist(mod)
            meta["cells"] = len(mod.get("cells", {}))
        out["svg"] = draw_view(jpath, lazy_dir / f"view{idx}.svg", meta["what"], meta["cells"], skin)
        done.write_text(json.dumps(out), encoding="utf-8")
        return out


def complete_viewer(lazy_dir: Path, out_html: Path) -> None:
    """A self-contained copy of a lazily drawn viewer: every schematic drawn and embedded."""
    data = json.loads((lazy_dir / "data.json").read_text(encoding="utf-8"))
    for v in data["views"].values():
        for e in v["modules"].values():
            for k in ("hier", "flat"):
                if k in e and "lazy" in e[k]:
                    r = render_lazy(lazy_dir, e[k].pop("lazy"))
                    e[k]["svg"] = r["svg"]
                    if r.get("net"):
                        e[k]["net"], e[k]["cells"] = r["net"], len(r["net"]["cells"])
    write_page(data, out_html, data.pop("_title", None))


def _single_module(netlist: dict[str, Any], name: str, mod: dict[str, Any]) -> dict[str, Any]:
    mod = dict(mod)
    mod["attributes"] = {**mod.get("attributes", {}), "top": TOP_ATTR}
    return {"creator": netlist.get("creator", ""), "modules": {name: mod}}


def strip_xml_decl(svg: str) -> str:
    return re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", svg)


def build_viewer(stage_netlists: dict[str, dict[str, Any]], out_html: Path, work_dir: Path, *,
                 yosys: str, skin: Path | None = None, title: str | None = None,
                 default_stage: str | None = None, default_flat: bool = False,
                 fsms: dict[str, Any] | Callable[[], dict[str, Any]] | None = None, lang: str = "auto",
                 skipped: dict[str, str] | None = None, lazy_dir: Path | None = None) -> None:
    """Write the viewer. `stage_netlists` maps stage name -> hierarchical Yosys JSON netlist.

    lazy_dir (launcher builds): schematics above LAZY_MIN_CELLS (except each stage's top) are not
    drawn now; their netlists go to lazy_dir and render_lazy() draws them when first opened."""
    if not stage_netlists:
        raise ValueError("no netlists")
    stages = list(stage_netlists)

    # Jobs: (stage, module, flat?) -> (svg, slim netlist)
    jobs: list[tuple[str, str, bool]] = []
    too_big: dict[int, int] = {}   # job -> cells, for views not drawn
    for st, net in stage_netlists.items():
        memo: dict[str, int] = {}
        for name in net["modules"]:
            n = len(net["modules"][name].get("cells", {}))
            if n > MAX_DRAW_CELLS:
                too_big[len(jobs)] = n
            jobs.append((st, name, False))
            if user_children(net["modules"], name):
                n = flat_cells(net["modules"], name, memo)
                if n > MAX_FLAT_CELLS:
                    too_big[len(jobs)] = n
                jobs.append((st, name, True))
    if too_big:
        log.info(f"{len(too_big)} view(s) too large to lay out are not drawn (above {MAX_DRAW_CELLS} cells, "
                 f"or flattened above {MAX_FLAT_CELLS}); their sub-modules are")
    lazy: set[int] = set()
    if lazy_dir is not None:
        shutil.rmtree(lazy_dir, ignore_errors=True)
        lazy_dir.mkdir(parents=True)
        tops = {st: find_top(net["modules"]) for st, net in stage_netlists.items()}
        total = sum(len(m.get("cells", {})) for net in stage_netlists.values() for m in net["modules"].values())
        if len(jobs) > LAZY_MIN_VIEWS or total > LAZY_MIN_CELLS:
            lazy = {idx for idx, (st, name, flat) in enumerate(jobs)
                    if idx not in too_big and (flat or name != tops[st])}
        if lazy:
            log.info(f"{len(lazy)} of {len(jobs)} schematics will be drawn when first opened (a big design)")

    def run(idx: int) -> tuple[str, dict[str, Any]]:
        st, name, flat = jobs[idx]
        net = stage_netlists[st]
        mod = net["modules"][name]
        if idx in too_big:
            if flat:   # the flattened netlist itself is not needed: no schematic, no simulation of it
                return placeholder_svg("big", too_big[idx]), {"cells": {}, "ports": {}, "names": {}, "skipped": True}
            return placeholder_svg("big", too_big[idx]), slim_netlist(mod)
        if flat and idx in lazy:   # flattened later, when opened (Yosys run in render_lazy)
            sub, root = reroot(net, name)
            (lazy_dir / f"view{idx}.sub.json").write_text(json.dumps(sub))
            (lazy_dir / f"view{idx}.meta.json").write_text(json.dumps(
                {"what": f"{name} (flattened)", "flat": True, "root": root, "yosys": yosys, "cells": 0,
                 "skin": str(skin.resolve()) if skin else None}))
            return None, None
        if flat:
            sub, root = reroot(net, name)
            sub = postprocess(yosys, work_dir, sub, root, flatten=True, cones=[], tag=f"flat{idx}")
            single = _single_module(sub, root, sub["modules"][root])
            mod = sub["modules"][root]
        else:
            single = _single_module(net, name, mod)
        what, cells = f"{name}{' (flattened)' if flat else ''}", len(mod.get("cells", {}))
        if idx in lazy:
            (lazy_dir / f"view{idx}.json").write_text(json.dumps(single))
            (lazy_dir / f"view{idx}.meta.json").write_text(json.dumps(
                {"what": what, "cells": cells, "skin": str(skin.resolve()) if skin else None}))
            return None, slim_netlist(mod)
        jpath = work_dir / f"view{idx}.json"
        jpath.write_text(json.dumps(single))
        return draw_view(jpath, work_dir / f"view{idx}.svg", what, cells, skin), slim_netlist(mod)

    log.info(f"Rendering {len(jobs)} view(s) for stage(s): {', '.join(stages)}...")
    workers = max(1, min(8, os.cpu_count() or 1, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, range(len(jobs))))

    views: dict[str, Any] = {}
    for idx, ((st, name, flat), (svg, slim)) in enumerate(zip(jobs, results)):
        net = stage_netlists[st]
        mod = net["modules"][name]
        view = views.setdefault(st, {"top": find_top(net["modules"]), "modules": {}})
        entry = view["modules"].setdefault(name, {
            "label": display_name(name, mod.get("attributes")),
            "src": mod.get("attributes", {}).get("src", ""),
            "children": user_children(net["modules"], name),
        })
        v = {"svg": svg} if svg is not None else {"lazy": idx}   # lazy: drawn by the launcher on demand
        if slim is None:   # a flattened view left for later: its netlist comes with the schematic
            mods_ = stage_netlists[st]["modules"]
            entry["flat"] = {**v, "net": None, "cells": flat_cells(mods_, name)}
        else:
            entry["flat" if flat else "hier"] = {**v, "net": slim, "cells": len(slim["cells"])}

    first = views[stages[0]]
    data = {
        "version": __version__,
        "stages": stages,
        "defaultStage": default_stage if default_stage in views else stages[0],
        "defaultFlat": default_flat,
        "sources": collect_sources(stage_netlists, [Path.cwd(), work_dir]),
        "fsms": (fsms() if callable(fsms) else fsms) or {},
        "lang": lang,
        "skipped": skipped or {},   # stage -> why it is not in this viewer
        "views": views,
    }
    top_label = first["modules"][first["top"]]["label"]
    if lazy_dir is not None and lazy:   # kept for complete_viewer() (the downloadable, self-contained copy)
        (lazy_dir / "data.json").write_text(json.dumps({**data, "_title": title or top_label}), encoding="utf-8")
    write_page(data, out_html, title or top_label)


def write_page(data: dict[str, Any], out_html: Path, title: str | None) -> None:
    views = data["views"]
    # '<' only occurs inside JSON strings, so escaping it keeps </script> and
    # <!-- in SVG text from ending/confusing the inline <script>.
    svg_bytes = sum(len(e[k].get("svg") or "") for v in views.values() for e in v["modules"].values()
                    for k in ("hier", "flat") if k in e)
    if svg_bytes > GZIP_MIN:
        # each schematic compressed on its own and unpacked by the viewer when it is shown: the browser
        # never holds the whole design's SVG text at once (V8 strings stop at ~512 MB)
        for v in views.values():
            for e in v["modules"].values():
                for k in ("hier", "flat"):
                    if k in e and len(e[k].get("svg") or "") > 20000:
                        e[k]["svgz"] = base64.b64encode(gzip.compress(e[k].pop("svg").encode("utf-8"), 6)).decode()
    payload = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    if len(payload) > GZIP_MIN:   # SVG text compresses ~10x; the browser unpacks it (DecompressionStream)
        packed = base64.b64encode(gzip.compress(payload.encode("utf-8"), 6)).decode("ascii")
        log.info(f"Viewer data compressed: {len(payload) / 1e6:.0f} MB -> {len(packed) / 1e6:.0f} MB")
        payload = json.dumps({"gz": packed})
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace(TITLE_PLACEHOLDER, _html_escape(title or "VeriView"))
    page = page.replace(SIM_PLACEHOLDER, SIM_JS.read_text(encoding="utf-8").replace("</", "<\\/"), 1)
    page = page.replace(TIMING_PLACEHOLDER, TIMING_JS.read_text(encoding="utf-8").replace("</", "<\\/"), 1)
    page = page.replace(DATA_PLACEHOLDER, payload)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(page, encoding="utf-8")
    log.info(f"Viewer saved: {out_html}")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
