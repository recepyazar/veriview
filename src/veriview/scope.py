"""Narrowing what is drawn: --select (sub-instance), --depth, --flatten and --cone.

All of this works on the hierarchical Yosys JSON netlist: the selected instance's
module (with its real parameters) becomes the new top, unreachable modules are
dropped, and -- if needed -- the result is fed back through Yosys (read_json) to
flatten and/or cut logic cones.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .log import VeriViewError

TOP_ATTR = "00000000000000000000000000000001"

# Register/latch cell types where --cone stops (the register itself is kept).
FF_TYPES = ",".join([
    "$ff", "$dff", "$dffe", "$adff", "$adffe", "$sdff", "$sdffe", "$sdffce", "$aldff", "$aldffe",
    "$dffsr", "$dffsre", "$dlatch", "$adlatch", "$dlatchsr",
])
SIGNAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.\[\]]*$")


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


def user_children(modules: dict[str, Any], name: str) -> dict[str, str]:
    """instance name -> module name, for instances of modules defined in the netlist."""
    return {c: i["type"] for c, i in modules[name].get("cells", {}).items() if i.get("type") in modules}


def resolve_instance(modules: dict[str, Any], top: str, path: str) -> tuple[str, list[str]]:
    """Resolve an instance path ('u_cpu/u_alu' or 'u_cpu.u_alu') to (module, instance names).

    Instance names may themselves contain dots (e.g. generate blocks:
    'fa_block[3].fa_inst'), so at each level the longest matching instance
    name followed by '/', '.' or the end of the path wins.
    """
    module, rest, insts = top, path.strip("/."), []
    while rest:
        kids = user_children(modules, module)
        match = None
        for inst in sorted(kids, key=len, reverse=True):
            if rest == inst or rest.startswith(inst + "/") or rest.startswith(inst + "."):
                match = inst
                break
        if match is None:
            names = sorted(kids)
            avail = ", ".join(names[:10]) + (f", ... ({len(names)} total)" if len(names) > 10 else "") or "none"
            where = "/".join(insts) or top
            raise VeriViewError(f"No sub-module instance '{rest}' in '{where}' (instances: {avail})")
        insts.append(match)
        module = kids[match]
        rest = rest[len(match):].lstrip("/.")
    return module, insts


def module_depths(modules: dict[str, Any], root: str) -> dict[str, int]:
    """Minimum instance depth of every module reachable from root (root = 0)."""
    depth = {root: 0}
    queue = [root]
    while queue:
        m = queue.pop(0)
        for child in user_children(modules, m).values():
            if child not in depth:
                depth[child] = depth[m] + 1
                queue.append(child)
    return depth


def _base_name(name: str, mod: dict[str, Any]) -> str:
    hdl = mod.get("attributes", {}).get("hdlname")
    if hdl:
        return hdl.lstrip("\\")  # older Yosys: '\\name'
    m = re.match(r"^\$paramod(?:\$[0-9a-fA-F]+)?\\([^\\]+)", name)
    return m.group(1) if m else name


def reroot(netlist: dict[str, Any], root: str, keep_below: int | None = None) -> tuple[dict[str, Any], str]:
    """Make `root` the top: drop unreachable modules, give root a plain name, and mark
    modules deeper than `keep_below` levels with keep_hierarchy (for --depth)."""
    modules = netlist["modules"]
    depths = module_depths(modules, root)
    out: dict[str, Any] = {}
    for name in modules:
        if name not in depths:
            continue
        mod = copy.deepcopy(modules[name])
        attrs = mod.setdefault("attributes", {})
        attrs.pop("top", None)
        if keep_below is not None and depths[name] > keep_below:
            attrs["keep_hierarchy"] = TOP_ATTR
        out[name] = mod

    # Rename a parameterized root ('$paramod\fa\W=..') to its plain name so Yosys
    # selections can address it; nothing references the root after pruning.
    new_root = root
    if root.startswith("$"):
        base = _base_name(root, out[root])
        new_root = base if base not in out else f"{base}_selected"
        out = {(new_root if k == root else k): v for k, v in out.items()}
    out[new_root]["attributes"]["top"] = TOP_ATTR
    return {"creator": netlist.get("creator", ""), "modules": out}, new_root


def postprocess(yosys: str, work_dir: Path, netlist: dict[str, Any], top: str, *,
                flatten: bool, cones: list[str], tag: str = "scope") -> dict[str, Any]:
    """Run Yosys on a JSON netlist to flatten and/or keep only the given logic cones."""
    if not flatten and not cones:
        return netlist
    for sig in cones:
        if not SIGNAL_RE.match(sig):
            raise VeriViewError(f"Invalid signal name for --cone: '{sig}'")
    src = work_dir / f"{tag}_in.json"
    dst = work_dir / f"{tag}_out.json"
    src.write_text(json.dumps(netlist))
    script = [f'read_json "{src}"', f"hierarchy -top {top}"]
    if flatten:
        script.append("flatten")
    if cones:
        for sig in cones:
            script.append(f"select -assert-min 1 {top}/w:{sig}")
        wires = " ".join(f"{top}/w:{sig}" for sig in cones)
        if len(cones) > 1:
            wires += " %u" * (len(cones) - 1)
        script += [
            f"select -set cone {wires} %ci*:-{FF_TYPES}",
            f"select -set cone @cone @cone %ci1:+{FF_TYPES}[Q] %u",
            f"delete {top}/* @cone %d",
        ]
    script += ["opt_clean -purge", f'write_json "{dst}"']
    ys = work_dir / f"{tag}.ys"
    ys.write_text("\n".join(script) + "\n")
    r = subprocess.run([yosys, "-q", "-s", str(ys)], capture_output=True, text=True)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).strip()
        m = re.search(r"selection contains 0 elements.*?: \S+/w:(\S+)", out)
        if m:
            raise VeriViewError(f"Signal '{m.group(1)}' not found in module '{top}' (--cone)")
        raise VeriViewError(f"Yosys post-processing failed.\n{out}")
    return json.loads(dst.read_text())
