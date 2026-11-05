"""Finite state machine extraction (Yosys fsm_detect / fsm_extract) for diagrams.

Yosys turns every recognised state register into a `$fsm` cell whose parameters
hold the state encodings and the complete transition table. This module decodes
those cells into a JSON-friendly description:

    {name, width, states: [{code, value, name, outputs}], reset, inputs: [{label, src}],
     outputs: [names], transitions: [{from, to, pattern, cond, outputs}]}

Condition labels are the original source expressions (sliced from the HDL using
the `src` attribute of the cell that drives each FSM control input).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# {pre_json}:   the netlist just before extraction (fsm_extract removes the state register
#               wire, and with it the enum value names stored on it)
# {force}:      setattr commands marking registers fsm_detect skipped only because recoding
#               would not pay off (we want to *see* those FSMs, not recode them)
# {detect_log}: fsm_detect's messages, to find such registers
FSM_STAGE_COMMANDS = ["proc", "opt -nodffe -nosdff", "write_json {pre_json}", "{force}",
                      "tee -q -o {detect_log} fsm_detect", "fsm_extract", "fsm_opt", "opt_clean"]

_NOT_MARKING_RE = re.compile(r"Not marking (\S+) as FSM state register:\s*\n\s*Users of register don't seem to "
                             r"benefit from recoding")


def recoding_rejects(detect_log: str) -> list[tuple[str, str]]:
    """(module, wire) of registers fsm_detect rejected only for the recoding heuristic."""
    out = []
    for m in _NOT_MARKING_RE.finditer(detect_log):
        mod, _, wire = m.group(1).rpartition(".")
        if mod and wire:
            out.append((mod, wire))
    return out


def force_commands(rejects: list[tuple[str, str]]) -> str:
    cmds = []
    for mod, wire in rejects:
        sel = f"{mod}/w:{wire}" if re.fullmatch(r"[A-Za-z_]\w*", mod) else f"w:{wire}"
        cmds.append(f'setattr -set fsm_encoding "auto" {sel}')
    return "; ".join(cmds)

_SRC_RE = re.compile(r"^(.*):(\d+)\.(\d+)-(\d+)\.(\d+)$")


class SourceReader:
    def __init__(self, search_dirs: list[Path]):
        self.dirs = search_dirs
        self.cache: dict[str, list[str] | None] = {}

    def lines(self, path: str) -> list[str] | None:
        if path not in self.cache:
            self.cache[path] = None
            for base in self.dirs:
                f = Path(path) if Path(path).is_absolute() else base / path
                if f.is_file():
                    self.cache[path] = f.read_text(encoding="utf-8", errors="replace").splitlines()
                    break
        return self.cache[path]

    def snippet(self, src: str | None) -> str | None:
        """Source text of a single-line src range ("file:l.c-l.c")."""
        for part in (src or "").split("|"):
            m = _SRC_RE.match(part)
            if not m:
                continue
            path, l1, c1, l2, c2 = m.group(1), *map(int, m.groups()[1:])
            lines = self.lines(path)
            if not lines or l1 != l2 or l1 > len(lines):
                continue
            text = lines[l1 - 1][c1 - 1:c2 - 1].strip()
            if text:
                return text
        return None

    def text(self, path: str) -> str:
        return "\n".join(self.lines(path) or [])


def _param_int(v: Any) -> int:
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s.replace("x", "0").replace("-", "0"), 2) if re.fullmatch(r"[01x\-]+", s) else int(s)


def _bits_lsb(v: Any) -> str:
    """Yosys constant (MSB-first string) -> LSB-first string of '0'/'1'/'-'."""
    return str(v)[::-1].replace("x", "-")


def _strip(name: str) -> str:
    return name[1:] if name.startswith("\\") else name


def _bit_names(mod: dict[str, Any]) -> dict[int, str]:
    names: dict[int, str] = {}
    for n, v in sorted(mod.get("netnames", {}).items(), key=lambda kv: (kv[1].get("hide_name", 0), len(kv[0]))):
        bits = v.get("bits", [])
        for i, b in enumerate(bits):
            if isinstance(b, int) and b not in names and not v.get("hide_name"):
                names[b] = n if len(bits) == 1 else f"{n}[{i}]"
    return names


def _state_names(mod: dict[str, Any], reg: str, width: int, reader: SourceReader) -> dict[int, str]:
    """Encoding -> name, from enum attributes or from localparams/parameters in the source."""
    net = mod.get("netnames", {}).get(reg) or mod.get("netnames", {}).get("\\" + reg) or {}
    attrs = net.get("attributes", {})
    out: dict[int, str] = {}
    for k, v in attrs.items():
        if k.startswith("enum_value_"):
            out[int(k[len("enum_value_"):].replace("x", "0"), 2)] = _strip(str(v))
    if out:
        return out
    # localparam IDLE = 2'b00, RUN = 2'd1;  /  parameter S0 = 0;
    files = {p.rsplit(":", 1)[0] for p in str(net.get("attributes", {}).get("src", "")).split("|") if ":" in p}
    decl = re.compile(r"\b(?:localparam|parameter)\b([^;]*);", re.S)
    item = re.compile(r"(?:\[[^\]]*\]\s*)?([A-Za-z_]\w*)\s*=\s*(\d*)'?\s*([sS]?[bBoOdDhH])?\s*([0-9a-fA-F_xXzZ]+)")
    for f in files:
        for m in decl.finditer(reader.text(f)):
            for it in item.finditer(m.group(1)):
                name, size, base, digits = it.groups()
                try:
                    radix = {"b": 2, "o": 8, "d": 10, "h": 16}[(base or "d")[-1].lower()]
                    val = int(digits.replace("_", ""), radix)
                except (ValueError, KeyError):
                    continue
                if val < (1 << width):
                    out.setdefault(val, name)
    return out


def extract_fsms(netlist: dict[str, Any], reader: SourceReader,
                 pre: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
    """`pre` is the netlist before fsm_extract (for enum/localparam state names)."""
    result: dict[str, list[dict[str, Any]]] = {}
    for mname, mod in netlist.get("modules", {}).items():
        pre_mod = (pre or {}).get("modules", {}).get(mname, mod)
        bit_names = _bit_names(mod)
        drivers: dict[int, dict[str, Any]] = {}
        for c in mod.get("cells", {}).values():
            dirs = c.get("port_directions", {})
            for p, bits in c.get("connections", {}).items():
                if dirs.get(p) == "output":
                    for b in bits:
                        if isinstance(b, int):
                            drivers.setdefault(b, c)
        for cname, cell in mod.get("cells", {}).items():
            if cell.get("type") != "$fsm":
                continue
            p = cell["parameters"]
            reg = _strip(str(p.get("NAME", cname)))
            sbits, snum, slog2 = (_param_int(p[k]) for k in ("STATE_BITS", "STATE_NUM", "STATE_NUM_LOG2"))
            nin, nout, ntr = (_param_int(p[k]) for k in ("CTRL_IN_WIDTH", "CTRL_OUT_WIDTH", "TRANS_NUM"))
            stable, ttable = _bits_lsb(p["STATE_TABLE"]), _bits_lsb(p["TRANS_TABLE"])
            ctrl_in = cell["connections"].get("CTRL_IN", [])
            ctrl_out = cell["connections"].get("CTRL_OUT", [])

            def num(bits_lsb: str) -> int:
                return int(bits_lsb[::-1].replace("-", "0") or "0", 2)

            names = _state_names(pre_mod, reg, sbits, reader)
            states = []
            for i in range(snum):
                code = stable[i * sbits:(i + 1) * sbits]
                val = num(code)
                states.append({"value": val, "code": f"{sbits}'b{code[::-1]}", "name": names.get(val, f"S{i}"),
                               "outputs": {}})

            inputs = []
            for b in ctrl_in:
                drv = drivers.get(b) if isinstance(b, int) else None
                label = bit_names.get(b) if isinstance(b, int) else None
                src = drv.get("attributes", {}).get("src") if drv else None
                expr = reader.snippet(src) if src else None
                inputs.append({"label": label or expr or (f"{drv['type']}" if drv else str(b)),
                               "expr": expr, "src": src or ""})
            # show named outputs only; internal decode nets and the next-state signal are not interesting
            out_names = [bit_names.get(b) if isinstance(b, int) else None for b in ctrl_out]
            next_re = re.compile(rf"^(next|nxt|{re.escape(reg)}_?(next|nxt|d))(_state)?(\[\d+\])?$")
            keep = [i for i, n in enumerate(out_names) if n and not next_re.match(n)]

            transitions = []
            w = nout + slog2 + nin + slog2
            for t in range(ntr):
                e = ttable[t * w:(t + 1) * w]
                cout, sout = e[:nout], e[nout:nout + slog2]
                cin, sin = e[nout + slog2:nout + slog2 + nin], e[nout + slog2 + nin:]
                transitions.append({
                    "from": num(sin), "to": num(sout), "cubes": [cin],
                    "outputs": {out_names[i]: int(cout[i]) for i in keep if cout[i] in "01"},
                })
            # Moore outputs: identical on every transition leaving a state
            for si, st in enumerate(states):
                leaving = [t for t in transitions if t["from"] == si]
                if leaving:
                    first = leaving[0]["outputs"]
                    common = {k: v for k, v in first.items() if all(t["outputs"].get(k) == v for t in leaving)}
                    st["outputs"] = common
                    for t in leaving:
                        t["outputs"] = {k: v for k, v in t["outputs"].items() if k not in common}
            transitions = _merge(transitions, [i["label"] for i in inputs])
            # STATE_RST is the *index* of the reset state (not its encoding); all x/- if there is none
            rst = str(p.get("STATE_RST", ""))
            reset = _param_int(rst) if re.fullmatch(r"[01]+", rst) or isinstance(p.get("STATE_RST"), int) else None
            if reset is not None and reset >= len(states):
                reset = None
            result.setdefault(mname, []).append({
                "name": reg, "width": sbits, "states": states, "reset": reset,
                "inputs": inputs, "outputs": [out_names[i] for i in keep], "transitions": transitions,
                "arst": _param_int(p.get("ARST_POLARITY", 0)) if "ARST" in cell["connections"] else None,
            })
    return result


def _minimize(cubes: list[str], n: int) -> list[str]:
    """Quine-McCluskey: minimal-ish sum of products for a set of cubes ('0'/'1'/'-', LSB first)."""
    if n == 0 or n > 10:
        return cubes
    minterms: set[str] = set()
    for c in cubes:
        todo = [c]
        while todo:
            x = todo.pop()
            i = x.find("-")
            if i < 0:
                minterms.add(x)
            else:
                todo += [x[:i] + "0" + x[i + 1:], x[:i] + "1" + x[i + 1:]]
    if len(minterms) == 1 << n:
        return ["-" * n]
    primes: set[str] = set()
    current = set(minterms)
    while current:
        nxt, used, lst = set(), set(), sorted(current)
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                diff = [k for k in range(n) if a[k] != b[k]]
                if len(diff) == 1 and "-" not in (a[diff[0]], b[diff[0]]):
                    nxt.add(a[:diff[0]] + "-" + a[diff[0] + 1:])
                    used.update((a, b))
        primes |= current - used
        current = nxt

    def covers(p: str, m: str) -> bool:
        return all(pc in ("-", mc) for pc, mc in zip(p, m))

    remaining, chosen = set(minterms), []
    while remaining:
        best = max(sorted(primes), key=lambda p: (sum(covers(p, m) for m in remaining), p.count("-")))
        chosen.append(best)
        remaining = {m for m in remaining if not covers(best, m)}
    return chosen


def _cond(cubes: list[str], labels: list[str]) -> str:
    sums = []
    for c in cubes:
        terms = []
        for i, ch in enumerate(c):
            if ch == "-":
                continue
            lab = labels[i]
            terms.append(lab if ch == "1" else (f"!{lab}" if re.fullmatch(r"[\w\[\].]+", lab) else f"!({lab})"))
        # stable order (by signal/expression, ignoring negation) so labels do not depend on Yosys' port order
        terms.sort(key=lambda t: t.lstrip("!(").lower())
        sums.append(" && ".join(terms) or "1")
    if "1" in sums:
        return "1"
    return " || ".join(f"({t})" if len(sums) > 1 and "&&" in t else t for t in sums)


def _merge(transitions: list[dict[str, Any]], labels: list[str]) -> list[dict[str, Any]]:
    """One arrow per (from, to) with a simplified condition; Mealy outputs kept when identical."""
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for t in transitions:
        key = (t["from"], t["to"])
        if key not in merged:
            merged[key] = {**t, "cubes": list(t["cubes"])}
            continue
        m = merged[key]
        m["cubes"] += t["cubes"]
        m["outputs"] = {k: v for k, v in m["outputs"].items() if t["outputs"].get(k) == v}
    out = []
    for m in merged.values():
        cubes = _minimize(m.pop("cubes"), len(labels))
        m["cond"] = _cond(cubes, labels)
        m["pattern"] = [c[::-1] for c in cubes]
        out.append(m)
    return sorted(out, key=lambda t: (t["from"], t["to"] != t["from"], t["to"]))


def format_fsms(fsms: dict[str, list[dict[str, Any]]]) -> str:
    if not fsms:
        return "No state machines found."
    lines = []
    for mod, lst in fsms.items():
        for f in lst:
            states = f["states"]
            lines.append(f"FSM {mod}.{f['name']}  ({len(states)} states, {f['width']}-bit state register, "
                         f"{len(f['transitions'])} transitions)")
            for i, s in enumerate(states):
                outs = ", ".join(f"{k}={v}" for k, v in s["outputs"].items())
                lines.append(f"  {'*' if i == f['reset'] else ' '} {s['name']:<16} {s['code']:<8} {outs}")
            lines.append("  transitions:")
            for t in f["transitions"]:
                outs = ", ".join(f"{k}={v}" for k, v in t["outputs"].items())
                lines.append(f"    {states[t['from']]['name']} -> {states[t['to']]['name']}  [{t['cond']}]"
                             + (f" / {outs}" if outs else ""))
            lines.append("")
    lines.append("(* = reset state)")
    return "\n".join(lines)


def to_dot(fsms: dict[str, list[dict[str, Any]]]) -> str:
    out = []
    for mod, lst in fsms.items():
        for f in lst:
            s = f["states"]
            out.append(f'digraph "{mod}.{f["name"]}" {{\n  rankdir=LR;\n  node [shape=circle];')
            for i, st in enumerate(s):
                shape = "doublecircle" if i == f["reset"] else "circle"
                outs = "\\n".join(f"{k}={v}" for k, v in st["outputs"].items() if v)
                out.append(f'  s{i} [shape={shape}, label="{st["name"]}' + (f"\\n{outs}" if outs else "") + '"];')
            for t in f["transitions"]:
                lab = t["cond"].replace('"', '\\"')
                out.append(f'  s{t["from"]} -> s{t["to"]} [label="{lab}"];')
            out.append("}")
    return "\n".join(out) + "\n"
