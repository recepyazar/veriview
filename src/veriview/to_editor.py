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
    return ["proc", "flatten", "synth -noshare -run coarse:fine", "memory_map", "techmap", "opt -fast",
            "dfflegalize -cell $_DFF_P_ 01 -cell $_DFF_PN0_ 01", f"abc -g {GATE_SET}", "opt_clean -purge"]


def _ident(name: str) -> str | None:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name.lstrip("\\")).strip("_")
    s = re.sub(r"__+", "_", s)
    if not s or s[0].isdigit() or s in KEYWORDS:
        return None
    return s


GATE2 = {"and", "or", "nand", "nor", "xor", "xnor"}
BLOCKS = {"add", "sub", "cmp", "reg", "cnt", "dec", "muxn", "split", "merge", "inst"}
SEQ = {"dff", "dffr", "reg", "cnt"}


def _pins(nd: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Input and output pins of an editor node (as editor_core.js pinsOf)."""
    t, n = nd["type"], nd.get("bits") or 4
    fixed = {"in": ([], ["Y"]), "const0": ([], ["Y"]), "const1": ([], ["Y"]), "const": ([], ["Y"]), "out": (["A"], []),
             "not": (["A"], ["Y"]), "buf": (["A"], ["Y"]), "mux": (["A", "B", "S"], ["Y"]),
             "dff": (["D", "CLK"], ["Q"]), "dffr": (["D", "CLK", "RST_N"], ["Q"]),
             "add": (["A", "B", "CIN"], ["S", "COUT"]),
             "sub": (["A", "B"], ["Y", "BOUT"]), "cmp": (["A", "B"], ["EQ", "LT", "GT"]),
             "reg": (["D", "CLK", "EN", "RST_N"], ["Q"]), "cnt": (["CLK", "EN", "RST_N"], ["Q"]), "dec": (["A"], ["Y"])}
    if t in fixed:
        return fixed[t]
    if t == "split":
        return ["A"], [f"B{i}" for i in range(n)]
    if t == "merge":
        return [f"B{i}" for i in range(n)], ["Y"]
    if t == "inst":
        ps = nd.get("ports", [])
        return [p["name"] for p in ps if p["dir"] != "output"], [p["name"] for p in ps if p["dir"] == "output"]
    return ["A", "B", "C", "D"][:nd.get("n", 2)], ["Y"]


def _size(nd: dict[str, Any]) -> tuple[int, int]:
    """Node size as drawn by editor.html (sizeOf), for the layout."""
    t = nd["type"]
    if t in BLOCKS:
        ins, outs = _pins(nd)
        h = max(50, 20 * max(len(ins), len(outs)) + 14)
        if t == "inst":
            longest = max([len(x) for x in ins + outs] or [0])
            return max(110, len(str(nd.get("module", ""))) * 8 + 30, longest * 12 + 40), h
        return (50 if t in ("split", "merge") else 90), h
    if t == "const":
        return 56, 30
    if t in SIZE:
        return SIZE[t]
    return 64, max(40, 20 * len(_pins(nd)[0]) + 4)


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
    ins = ["A", "B"] if t == "mux" else _pins(nd)[0]
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
        elif n["type"] in SEQ:
            level[n["id"]] = 1
    gates = [n for n in nodes if n["type"] not in ("in", "out", "const0", "const1", "const") and n["type"] not in SEQ]

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
        if n["type"] == "out":
            level[n["id"]] = last
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


# ---- blocks: the module with the editor's blocks (adders, comparators, registers, MUX, buses) ----
# Coarse Yosys cells with an editor block; every other cell is mapped to gates (stage "editor-blocks")
COARSE = ("$and", "$or", "$xor", "$xnor", "$not", "$mux", "$add", "$sub", "$eq", "$ne", "$lt", "$le", "$gt", "$ge",
          "$dff", "$adff", "$dffe", "$adffe")
MAX_EDITOR_BUS = 64


def blocks_commands() -> list[str]:
    from .synth import GATE_SET
    keep = " ".join(f"*/t:{t} %d" for t in COARSE)
    signed_cmp = "*/t:$lt */t:$le %u */t:$gt %u */t:$ge %u */r:A_SIGNED=1 %i"   # the editor's CMP is unsigned
    # arithmetic helper cells the first techmap leaves behind (multipliers -> $macc -> $fa / $lcu ...)
    helpers = "*/t:$fa */t:$lcu %u */t:$alu %u */t:$macc %u */t:$macc_v2 %u */t:$pos %u */t:$buf %u"
    return ["proc", "opt", "wreduce", "opt_clean", "memory -nomap", "memory_map", "opt_clean",
            f"techmap {signed_cmp}", f"techmap */t:$* {keep} */t:$_* %d */t:$scopeinfo %d", f"techmap {helpers}",
            "dfflegalize -cell $_DFF_P_ 01 -cell $_DFF_PN0_ 01 "
            "*/t:$_DFF* */t:$_SDFF* %u */t:$_ALDFF* %u */t:$_DLATCH* %u",
            f"abc -g {GATE_SET}", "opt_clean -purge"]


def netlist_to_blocks(netlist: dict[str, Any], module: str | None = None) -> dict[str, Any]:
    """The module drawn with editor blocks; sub-module instances stay module blocks."""
    from .html import _PARAMOD_RE, decode_param
    name, mod = (module, netlist["modules"][module]) if module else top_module(netlist)
    mods = netlist["modules"]
    nodes: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    consts: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    warn: list[str] = []
    seq = [1]

    def new_node(**kw: Any) -> dict[str, Any]:
        nd = {"id": seq[0], "x": 0, "y": 0, **kw}
        seq[0] += 1
        nodes.append(nd)
        return nd

    def wire(a: dict[str, Any], ap: str, b: dict[str, Any], bp: str) -> None:
        wires.append({"id": 0, "from": {"node": a["id"], "pin": ap}, "to": {"node": b["id"], "pin": bp}})

    drv: dict[int, tuple[dict[str, Any], str, int, int]] = {}   # bit -> (node, pin, index, pin width)
    zero_bits: set[int] = set()                                   # bits that are constant 0 (e.g. Y[1:] of $eq)
    splits: dict[tuple[int, str], dict[str, Any]] = {}
    pending: list[tuple[list[Any], dict[str, Any], str]] = []

    def add_out(nd: dict[str, Any], pin: str, bits: list[Any]) -> None:
        for i, b in enumerate(bits):
            if isinstance(b, int):
                drv[b] = (nd, pin, i, len(bits))

    def check_width(n: int, what: str) -> None:
        if n > MAX_EDITOR_BUS:
            raise VeriViewError(f"{what}: {n} bits; the gate editor's buses have at most {MAX_EDITOR_BUS}")

    def const_node(bits: list[Any], sink: dict[str, Any], pin: str) -> dict[str, Any]:
        vals = [1 if b == "1" else 0 for b in bits]
        if len(bits) == 1:
            cn = new_node(type="const1" if vals[0] else "const0")
        else:
            cn = new_node(type="const", bits=len(bits), value=sum(v << i for i, v in enumerate(vals)))
        consts.append((cn, sink, pin))
        return cn

    def bit_src(b: Any, sink: dict[str, Any], pin: str) -> tuple[dict[str, Any], str]:
        if not isinstance(b, int) or b in zero_bits or b not in drv:
            return const_node([b if not isinstance(b, int) else "0"], sink, pin), "Y"
        nd, p, i, w = drv[b]
        if w == 1:
            return nd, p
        key = (nd["id"], p)
        if key not in splits:
            check_width(w, "a bus")
            sp = new_node(type="split", bits=w)
            wire(nd, p, sp, "A")
            splits[key] = sp
        return splits[key], f"B{i}"

    def connect(bits: list[Any], dst: dict[str, Any], dpin: str) -> None:
        pending.append((list(bits), dst, dpin))

    def resolve(bits: list[Any], dst: dict[str, Any], dpin: str) -> None:
        n = len(bits)
        if not n:
            return
        if all(not isinstance(b, int) or b in zero_bits for b in bits):
            wire(const_node([b if not isinstance(b, int) else "0" for b in bits], dst, dpin), "Y", dst, dpin)
            return
        first = drv.get(bits[0]) if isinstance(bits[0], int) else None
        if first and first[3] == n and all(isinstance(b, int) and drv.get(b) == (first[0], first[1], i, n)
                                           for i, b in enumerate(bits)):
            wire(first[0], first[1], dst, dpin)      # the whole output of one node, in order
            return
        if n == 1:
            src, sp = bit_src(bits[0], dst, dpin)
            wire(src, sp, dst, dpin)
            return
        check_width(n, "a bus")
        mg = new_node(type="merge", bits=n)
        shared: dict[int, dict[str, Any]] = {}   # one 0 and one 1 per merger, not one per bit
        for i, b in enumerate(bits):
            if not isinstance(b, int) or b in zero_bits or b not in drv:
                v = 1 if b == "1" else 0
                if v not in shared:
                    shared[v] = const_node(["1" if v else "0"], mg, f"B{i}")
                wire(shared[v], "Y", mg, f"B{i}")
                continue
            src, sp = bit_src(b, mg, f"B{i}")
            wire(src, sp, mg, f"B{i}")
        wire(mg, "Y", dst, dpin)

    def ext(bits: list[Any], w: int, signed: bool) -> list[Any]:
        bits = list(bits[:w])
        fill = bits[-1] if signed and bits else "0"
        return bits + [fill] * (w - len(bits))

    def inverted(bits: list[Any], dst: dict[str, Any], dpin: str) -> None:
        nt = new_node(type="not")
        connect(bits, nt, "A")
        wire(nt, "Y", dst, dpin)

    def pint(c: dict[str, Any], k: str, d: int = 0) -> int:
        v = c.get("parameters", {}).get(k)
        if v is None:
            return d
        v = str(v)
        return int(v, 2) if re.fullmatch(r"[01]+", v) else int(v) if v.lstrip("-").isdigit() else d

    # ports
    port_names: set[str] = set()
    for pname, p in mod.get("ports", {}).items():
        bits, base = p.get("bits", []), _ident(pname) or "p"
        check_width(len(bits), f"port {pname}")
        port_names.add(base)
        if p.get("direction") == "input":
            nd = new_node(type="in", name=base, **({"bits": len(bits)} if len(bits) > 1 else {}))
            add_out(nd, "Y", bits)
        else:
            if p.get("direction") == "inout":
                warn.append(f"inout port {pname} drawn as an output")
            connect(bits, new_node(type="out", name=base), "A")

    # cells
    for cname, c in sorted(mod.get("cells", {}).items()):
        t, conn = c["type"], c.get("connections", {})
        if t in CELLS:   # 1-bit gates from the cells mapped to gates
            etype, pins, out = CELLS[t]
            nd = new_node(type=etype, **({"n": 2} if etype in GATE2 else {}))
            for yp, bits in conn.items():
                if yp == out:
                    add_out(nd, OUT_PIN[etype], bits)
                elif yp in pins:
                    connect(bits, nd, pins[yp])
            continue
        if t in mods:    # a sub-module instance: a module block
            child = mods[t]
            m = _PARAMOD_RE.match(t)
            hdl = child.get("attributes", {}).get("hdlname")
            base = (hdl or (m.group(1) if m else t.split("$", 1)[0])).lstrip("\\")
            params = {}
            if m and m.group(2):
                for item in m.group(2).split("\\"):
                    k, _, v = item.partition("=")
                    if k:
                        params[k] = decode_param(v)
            src = str(child.get("attributes", {}).get("src", "")).split("|")[0].rsplit(":", 1)[0]
            ports = [{"name": pn, "dir": "output" if pp.get("direction") == "output" else "input",
                      "bits": len(pp.get("bits", []))} for pn, pp in child.get("ports", {}).items()]
            nd = new_node(type="inst", module=base, file=src, ports=ports, label=_ident(cname) or None,
                          **({"params": params} if params else {}))
            if not nd["label"]:
                del nd["label"]
            for pp in ports:
                bits = conn.get(pp["name"], [])
                if pp["dir"] == "output":
                    add_out(nd, pp["name"], bits)
                else:
                    connect(bits, nd, pp["name"])
            continue
        if t in ("$scopeinfo",) or t.startswith(("$print", "$check", "$assert", "$assume", "$cover")):
            continue
        sa, sb = bool(pint(c, "A_SIGNED")), bool(pint(c, "B_SIGNED"))
        if t in ("$and", "$or", "$xor", "$xnor", "$add", "$sub"):
            w = len(conn["Y"])
            check_width(w, t)
            typ = {"$add": "add", "$sub": "sub"}.get(t, t[1:])
            nd = new_node(type=typ, bits=w, **({"n": 2} if typ in GATE2 else {}))
            connect(ext(conn["A"], w, sa), nd, "A")
            connect(ext(conn["B"], w, sb), nd, "B")
            add_out(nd, {"add": "S", "sub": "Y"}.get(typ, "Y"), conn["Y"])
        elif t == "$not":
            w = len(conn["Y"])
            check_width(w, t)
            nd = new_node(type="not", bits=w)
            connect(ext(conn["A"], w, sa), nd, "A")
            add_out(nd, "Y", conn["Y"])
        elif t == "$mux":
            w = len(conn["Y"])
            check_width(w, t)
            nd = new_node(type="mux", bits=w)
            for p in ("A", "B", "S"):
                connect(conn[p], nd, p)
            add_out(nd, "Y", conn["Y"])
        elif t in ("$eq", "$ne", "$lt", "$le", "$gt", "$ge"):
            k = max(len(conn["A"]), len(conn["B"]))
            check_width(k, t)
            nd = new_node(type="cmp", bits=k)
            connect(ext(conn["A"], k, sa and sb), nd, "A")
            connect(ext(conn["B"], k, sa and sb), nd, "B")
            pin, neg = {"$eq": ("EQ", False), "$ne": ("EQ", True), "$lt": ("LT", False), "$ge": ("LT", True),
                        "$gt": ("GT", False), "$le": ("GT", True)}[t]
            y = conn["Y"]
            if neg:
                nt = new_node(type="not")
                wire(nd, pin, nt, "A")
                add_out(nt, "Y", y[:1])
            else:
                add_out(nd, pin, y[:1])
            zero_bits.update(b for b in y[1:] if isinstance(b, int))
        elif t in ("$dff", "$adff", "$dffe", "$adffe"):
            w = pint(c, "WIDTH", len(conn["Q"]))
            check_width(w, t)
            has_rst, has_en = t.startswith("$adff"), t.endswith("e")
            typ = "reg" if has_en else ("dffr" if has_rst else "dff")
            nd = new_node(type=typ, bits=w)
            (inverted if not pint(c, "CLK_POLARITY", 1) else connect)(conn["CLK"], nd, "CLK")
            if has_en:
                (inverted if not pint(c, "EN_POLARITY", 1) else connect)(conn["EN"], nd, "EN")
            rv = 0
            if has_rst:
                (connect if not pint(c, "ARST_POLARITY", 1) else inverted)(conn["ARST"], nd, "RST_N")
                rv = pint(c, "ARST_VALUE", 0)
            if rv:   # the editor's registers reset to 0: store D ^ R and read Q ^ R
                kn = new_node(type="const", bits=w, value=rv)
                xd, xq = new_node(type="xor", bits=w, n=2), new_node(type="xor", bits=w, n=2)
                connect(conn["D"], xd, "A")
                wire(kn, "Y", xd, "B")
                wire(xd, "Y", nd, "D")
                wire(nd, "Q", xq, "A")
                wire(kn, "Y", xq, "B")
                add_out(xq, "Y", conn["Q"])
            else:
                connect(conn["D"], nd, "D")
                add_out(nd, "Q", conn["Q"])
        else:
            raise VeriViewError(f"Cannot draw {t} ({cname}) in the gate editor")
    for bits, dst, dpin in pending:
        resolve(bits, dst, dpin)
    gates = sum(1 for n in nodes if n["type"] not in ("in", "out", "const0", "const1", "const"))
    if gates > MAX_CELLS:
        raise VeriViewError(f"{gates} blocks and gates: too big for the gate editor (limit {MAX_CELLS})")

    # public net names -> names of the blocks driving them (whole outputs only)
    taken = set(port_names) | {n["label"] for n in nodes if n.get("label")}
    for nname, v in sorted(mod.get("netnames", {}).items(), key=lambda kv: (kv[1].get("hide_name", 0), kv[0])):
        bits = v.get("bits", [])
        if v.get("hide_name") or nname.startswith("$") or not bits or bits[0] not in drv:
            continue
        nd, pin, _, w = drv[bits[0]]
        if (w != len(bits) or nd.get("label") or nd["type"] in ("in", "split", "merge", "cmp", "inst")
                or pin != _pins(nd)[1][0] or any(drv.get(b) != (nd, pin, i, w) for i, b in enumerate(bits))):
            continue
        label = _ident(nname)
        if label and label not in taken:
            nd["label"] = label
            taken.add(label)
    for w in wires:
        w["id"] = seq[0]
        seq[0] += 1
    _layout(nodes, wires, consts)
    return {"module": _ident(name.split("$", 1)[0]) or "design", "nodes": nodes, "wires": wires, "seq": seq[0],
            "warn": warn}
