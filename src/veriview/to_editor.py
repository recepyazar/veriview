"""Turn a module into a gate-editor drawing ("open in editor" in the viewer).

The module is synthesized to the editor's primitives (stage "editor" in synth.py:
flattened, 2-input gates + MUX + NOT/BUF, flip-flops legalized to plain D flip-flops
with or without an active-low asynchronous reset), then converted to the editor's
JSON model and laid out in columns (inputs | registers | logic by depth | outputs).

Multi-bit ports become one editor port per bit (`count_0`, `count_1`, ...); public
net names become gate labels, so the drawing keeps the names of the RTL.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from typing import Any

from .log import VeriViewError

MAX_CELLS = 1500
MAX_BUS = 32

# yosys cell type -> (editor type, {yosys pin: editor pin}, output pin)
CELLS: dict[str, tuple[str, dict[str, str], str]] = {
    "$_AND_": ("and", {"A": "A", "B": "B"}, "Y"),
    "$_NAND_": ("nand", {"A": "A", "B": "B"}, "Y"),
    "$_OR_": ("or", {"A": "A", "B": "B"}, "Y"),
    "$_NOR_": ("nor", {"A": "A", "B": "B"}, "Y"),
    "$_XOR_": ("xor", {"A": "A", "B": "B"}, "Y"),
    "$_XNOR_": ("xnor", {"A": "A", "B": "B"}, "Y"),
    "$_NOT_": ("not", {"A": "A"}, "Y"),
    "$_BUF_": ("buf", {"A": "A"}, "Y"),
    "$_MUX_": ("mux", {"A": "A", "B": "B", "S": "S"}, "Y"),
    "$_DFF_P_": ("dff", {"D": "D", "C": "CLK"}, "Q"),
    "$_DFF_PN0_": ("dffr", {"D": "D", "C": "CLK", "R": "RST_N"}, "Q"),
}
OUT_PIN = {"and": "Y", "nand": "Y", "or": "Y", "nor": "Y", "xor": "Y", "xnor": "Y", "not": "Y", "buf": "Y",
           "mux": "Y", "dff": "Q", "dffr": "Q", "in": "Y", "const0": "Y", "const1": "Y"}
# node sizes as drawn by editor.html (for the layout only)
SIZE = {"in": (70, 30), "out": (70, 30), "const0": (36, 30), "const1": (36, 30), "dff": (70, 64), "dffr": (70, 80),
        "mux": (50, 70), "not": (64, 40), "buf": (64, 40)}
KEYWORDS = set("""always and assign automatic begin buf case casex casez cell config deassign default defparam design
disable edge else end endcase endconfig endfunction endgenerate endmodule endprimitive endspecify endtable endtask
event for force forever fork function generate genvar if initial inout input integer join large localparam macromodule
medium module nand negedge nmos nor not or output parameter posedge reg release repeat signed small specify specparam
strong0 strong1 supply0 supply1 table task time tri tri0 tri1 triand trior trireg unsigned use wait wand while wire wor
xnor xor logic bit byte int""".split())

ROW_GAP, X0, Y0, TRACK = 30, 80, 80, 10


def editor_commands() -> list[str]:
    from .synth import GATE_SET
    return ["proc", "flatten", "synth -run coarse:fine", "memory_map", "techmap", "opt -fast",
            "dfflegalize -cell $_DFF_P_ 01 -cell $_DFF_PN0_ 01", f"abc -g {GATE_SET}", "opt_clean -purge"]


def _ident(name: str) -> str | None:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name.lstrip("\\")).strip("_")
    s = re.sub(r"__+", "_", s)
    if not s or s[0].isdigit() or s in KEYWORDS:
        return None
    return s


def _size(nd: dict[str, Any]) -> tuple[int, int]:
    if nd["type"] in ("split", "merge"):
        return 50, max(50, 20 * nd["bits"] + 14)
    return SIZE.get(nd["type"], (64, 44))


def top_module(netlist: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mods = netlist.get("modules", {})
    for name, m in mods.items():
        if str(m.get("attributes", {}).get("top", "")).strip("0"):
            return name, m
    if len(mods) == 1:
        return next(iter(mods.items()))
    raise VeriViewError("No top module in the netlist")


def netlist_to_design(netlist: dict[str, Any], module: str | None = None) -> dict[str, Any]:
    name, mod = (module, netlist["modules"][module]) if module else top_module(netlist)
    cells = mod.get("cells", {})
    bad = sorted({c["type"] for c in cells.values() if c["type"] not in CELLS})
    if bad:
        raise VeriViewError("Cannot draw these cells in the gate editor: " + ", ".join(bad) +
                            " (latches, memories and instances of black boxes are not supported)")
    if len(cells) > MAX_CELLS:
        raise VeriViewError(f"{len(cells)} gates: too big for the gate editor (limit {MAX_CELLS})")

    nodes: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    seq = [1]
    warn: list[str] = []

    def new_node(**kw: Any) -> dict[str, Any]:
        nd = {"id": seq[0], "x": 0, "y": 0, **kw}
        seq[0] += 1
        nodes.append(nd)
        return nd

    driver: dict[int, tuple[dict[str, Any], str]] = {}   # bit -> (node, output pin)
    sinks: list[tuple[Any, dict[str, Any], str]] = []    # (bit or constant, node, input pin)
    port_names: set[str] = set()
    split: list[str] = []
    direct: list[tuple[dict[str, Any], str, dict[str, Any], str]] = []   # wires between port and splitter/merger

    # buses: input -> splitter (one output per bit), merger -> output; wider than 32 bits: one port per bit
    for pname, p in mod.get("ports", {}).items():
        bits = p.get("bits", [])
        base = _ident(pname) or "p"
        is_in = p.get("direction") == "input"
        if p.get("direction") == "inout":
            warn.append(f"inout port {pname} drawn as an output")
        if 1 < len(bits) <= MAX_BUS:
            port_names.add(base)
            if is_in:
                pn = new_node(type="in", name=base, bits=len(bits))
                sp = new_node(type="split", bits=len(bits))
                direct.append((pn, "Y", sp, "A"))
                for i, b in enumerate(bits):
                    if isinstance(b, int):
                        driver.setdefault(b, (sp, f"B{i}"))
            else:
                mg = new_node(type="merge", bits=len(bits))
                pn = new_node(type="out", name=base)
                direct.append((mg, "Y", pn, "A"))
                for i, b in enumerate(bits):
                    sinks.append((b, mg, f"B{i}"))
            continue
        if len(bits) > 1:
            split.append(pname)
        for i, b in enumerate(bits):
            nm = base if len(bits) == 1 else f"{base}_{i}"
            port_names.add(nm)
            if is_in:
                nd = new_node(type="in", name=nm)
                if isinstance(b, int):
                    driver.setdefault(b, (nd, "Y"))
            else:
                nd = new_node(type="out", name=nm)
                sinks.append((b, nd, "A"))
    for cname, c in sorted(cells.items()):
        etype, pins, out = CELLS[c["type"]]
        nd = new_node(type=etype, **({"n": 2} if etype in ("and", "nand", "or", "nor", "xor", "xnor") else {}))
        nd["_cell"] = cname
        for ypin, bits in c.get("connections", {}).items():
            if ypin == out:
                if bits and isinstance(bits[0], int):
                    driver[bits[0]] = (nd, OUT_PIN[etype])
            elif ypin in pins:
                sinks.append((bits[0] if bits else "x", nd, pins[ypin]))
    if split:
        warn.append(f"ports wider than {MAX_BUS} bits split into one port per bit: " + ", ".join(split))

    # public net names -> labels of the gates that drive them
    taken = set(port_names)
    for nname, v in sorted(mod.get("netnames", {}).items(), key=lambda kv: (kv[1].get("hide_name", 0), kv[0])):
        if v.get("hide_name") or nname.startswith("$"):
            continue
        bits = v.get("bits", [])
        for i, b in enumerate(bits):
            d = driver.get(b) if isinstance(b, int) else None
            if not d or d[0]["type"] in ("in", "split") or d[0].get("label"):
                continue
            label = _ident(nname if len(bits) == 1 else f"{nname}_{i}")
            if label and label not in taken:
                d[0]["label"] = label
                taken.add(label)

    # wires (constants get their own small node next to the pin)
    consts: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for b, nd, pin in sinks:
        if isinstance(b, int):
            d = driver.get(b)
            if d:
                wires.append({"id": 0, "from": {"node": d[0]["id"], "pin": d[1]}, "to": {"node": nd["id"], "pin": pin}})
            continue
        cn = new_node(type="const1" if b == "1" else "const0")
        consts.append((cn, nd, pin))
        wires.append({"id": 0, "from": {"node": cn["id"], "pin": "Y"}, "to": {"node": nd["id"], "pin": pin}})
    for a, ap, b, bp in direct:
        wires.append({"id": 0, "from": {"node": a["id"], "pin": ap}, "to": {"node": b["id"], "pin": bp}})
    for w in wires:
        w["id"] = seq[0]
        seq[0] += 1

    _layout(nodes, wires, consts)
    for nd in nodes:
        nd.pop("_cell", None)
    return {"module": _ident(name) or "design", "nodes": nodes, "wires": wires, "seq": seq[0], "warn": warn}


def _in_pin(nd: dict[str, Any], pin: str) -> tuple[float, float]:
    """Input pin position, as editor.html's pinPos()."""
    w, h = _size(nd)
    t = nd["type"]
    if t == "mux" and pin == "S":
        return nd["x"] + w / 2, nd["y"] + h - 6
    if t == "merge":
        return nd["x"], nd["y"] + h * (int(pin[1:]) + 1) / (nd["bits"] + 1)
    ins = {"mux": ["A", "B"], "dff": ["D", "CLK"], "dffr": ["D", "CLK", "RST_N"], "out": ["A"],
           "not": ["A"], "buf": ["A"]}.get(t, ["A", "B", "C", "D"][:nd.get("n", 2)])
    i = ins.index(pin) if pin in ins else 0
    return nd["x"], nd["y"] + h * (i + 1) / (len(ins) + 1)


def _layout(nodes: list[dict[str, Any]], wires: list[dict[str, Any]], consts: list) -> None:
    """Columns: inputs | registers | logic by depth | outputs.

    Each wire's adjustable segment gets its own channel, so wires do not run on top of
    each other: forward wires turn in the gap before their target column (one vertical
    track per driving net), wires going backwards (into registers) run below the drawing
    (one horizontal track per net).
    """
    by_id = {n["id"]: n for n in nodes}
    const_ids = {c[0]["id"] for c in consts}
    preds: dict[int, list[int]] = defaultdict(list)
    for w in wires:
        if w["from"]["node"] not in const_ids:
            preds[w["to"]["node"]].append(w["from"]["node"])
    level: dict[int, int] = {}
    for n in nodes:
        if n["type"] == "in":
            level[n["id"]] = 0
        elif n["type"] in ("dff", "dffr", "split"):
            level[n["id"]] = 1
    gates = [n for n in nodes if n["type"] not in ("in", "out", "dff", "dffr", "const0", "const1", "split", "merge")]

    # longest path from inputs / register outputs (bounded: tolerates combinational loops)
    def lv(nid: int, stack: set[int]) -> int:
        if nid in level:
            return level[nid]
        if nid in stack:
            return 1
        stack.add(nid)
        v = 1 + max((lv(p, stack) for p in preds[nid]), default=0)
        stack.discard(nid)
        level[nid] = max(v, 1)
        return level[nid]

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 10000))
    try:
        for g in gates:
            lv(g["id"], set())
    finally:
        sys.setrecursionlimit(old)
    last = max(level.values(), default=0) + 1
    for n in nodes:
        if n["type"] == "merge":
            level[n["id"]] = last
        elif n["type"] == "out":
            level[n["id"]] = last + 1
    cols: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        if n["id"] in level:
            cols[level[n["id"]]].append(n)

    # forward nets entering each column -> width of the routing gap in front of it
    fwd: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for w in wires:
        a, b = w["from"]["node"], w["to"]["node"]
        if a in const_ids or a not in level or b not in level or level[b] <= level[a]:
            continue
        key = (a, w["from"]["pin"])
        if key not in fwd[level[b]]:
            fwd[level[b]].append(key)
    col_x: dict[int, float] = {}
    x = X0
    for c in sorted(cols):
        if c:
            x += max(70, TRACK * (len(fwd[c]) + 1)) + (40 if any(n["id"] in const_ids for n in cols[c]) else 0)
        col_x[c] = x
        x += max(_size(n)[0] for n in cols[c])

    # order each column by the mean position of its drivers (a single forward sweep)
    rank: dict[int, float] = {}
    bottom = Y0
    for c in sorted(cols):
        col = cols[c]
        if c > 0:
            def key(n: dict[str, Any]) -> float:
                ps = [rank[p] for p in preds[n["id"]] if p in rank]
                return sum(ps) / len(ps) if ps else float(len(rank))
            col.sort(key=key)
        y = Y0
        for i, n in enumerate(col):
            h = _size(n)[1]
            n["x"], n["y"] = col_x[c], y
            rank[n["id"]] = y + h / 2 + i * 1e-3
            y += h + ROW_GAP + (8 if n.get("label") else 0)
        bottom = max(bottom, y)
    for cn, sink, pin in consts:
        px, py = _in_pin(sink, pin)
        cn["x"], cn["y"] = px - 56, py - 15
    for n in nodes:
        n["x"], n["y"] = int(round(n["x"] / 10) * 10), int(round(n["y"] / 10) * 10)

    # channels
    gap_tracks: dict[tuple[int, tuple[int, str]], float] = {}
    for c, keys in fwd.items():
        gap_r = col_x[c] - (40 if any(n["id"] in const_ids for n in cols[c]) else 0)
        n_t = len(keys)
        width = max(70, TRACK * (n_t + 1))
        # nets from higher sources turn further left: fewer crossings
        keys = sorted(keys, key=lambda k: by_id[k[0]]["y"])
        for i, k in enumerate(keys):
            gap_tracks[(c, k)] = round(gap_r - width + width * (i + 1) / (n_t + 1))
    back: dict[tuple[int, str], float] = {}
    for w in wires:
        a, b = w["from"]["node"], w["to"]["node"]
        if a in const_ids or a not in level or b not in level:
            continue
        key = (a, w["from"]["pin"])
        if level[b] > level[a]:
            w["mx"] = gap_tracks[(level[b], key)]
        else:
            if key not in back:
                back[key] = bottom + 10 + TRACK * len(back)
            w["my"] = back[key]
