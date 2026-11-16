"""Finding what a set of sources is missing: packages, sub-modules and `include files.

Opening a folder (or a few files) of a bigger project usually misses files that live
elsewhere in the project: a vendored IP (`import fpnew_pkg::*;` from third_party/...),
a common module library, the folder of an `include. The project knows them from its
Makefile; VeriView only sees the files it was given. Here the missing names are looked
up in the project (the git work tree around the sources) and the files defining them
are added, transitively. Every addition is reported, and a file list (.f) or explicit
options can always be used instead.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

from . import log

HDL = (".v", ".sv", ".svh", ".vh")
SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}
MAX_INDEX_FILES = 20000
MAX_FILE_BYTES = 4_000_000
MAX_ADDED = 400

_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_DEF_PKG = re.compile(r"^[ \t]*package\s+(?:automatic\s+|static\s+)?(\w+)\s*;", re.M)
_DEF_MOD = re.compile(r"^[ \t]*(?:macromodule|module|interface|program)\s+(?:automatic\s+|static\s+)?(\w+)", re.M)
_USE_PKG = re.compile(r"\b([A-Za-z_]\w*)\s*::")
# `name #(` or `name inst (` / `name inst [N:0] (` at the start of a statement
_USE_MOD = re.compile(r"(?:^|[;\n])\s*([A-Za-z_]\w*)\b\s*(?:#\s*\(|[A-Za-z_]\w*\b\s*(?:\[[^\]]*\]\s*)*\()")
_INCLUDE = re.compile(r'`include\s+"([^"]+)"')
_KEYWORDS = set("""always always_comb always_ff always_latch and assert assign assume automatic begin bind case
casex casez cover default disable else end endcase endfunction endmodule endtask for force foreach forever
function generate if
initial input inout module nand nor not or output package parameter localparam program repeat return task typedef wait
while wire xor xnor logic reg bit int integer genvar import export interface modport property sequence class struct
union enum unique priority std""".split())


def _clean(text: str) -> str:
    return _COMMENT.sub(" ", text)


Scan = tuple[frozenset, frozenset, frozenset, frozenset, tuple]


@functools.lru_cache(maxsize=4096)
def _scan(path: str, mtime: float) -> Scan:
    """(packages defined, modules defined, packages used, possible module uses, includes) of a file."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset(), frozenset(), frozenset(), frozenset(), ()
    incs = tuple(_INCLUDE.findall(raw))
    text = _STRING.sub('""', _clean(raw))
    return (frozenset(_DEF_PKG.findall(text)), frozenset(_DEF_MOD.findall(text)),
            frozenset(_USE_PKG.findall(text)) - _KEYWORDS, frozenset(_USE_MOD.findall(text)) - _KEYWORDS, incs)


def scan(path: Path) -> Scan:
    try:
        return _scan(str(path), path.stat().st_mtime)
    except OSError:
        return frozenset(), frozenset(), frozenset(), frozenset(), ()


# folders that count as a project even without git (the launcher registers its start folder)
EXTRA_ROOTS: list[Path] = []


def project_root(paths: list[Path]) -> Path | None:
    for p in paths[:1]:
        parents = Path(p).resolve().parents
        for d in parents:
            if (d / ".git").exists():
                return d
        for r in EXTRA_ROOTS:
            if r in parents:
                return r
    return None


@functools.lru_cache(maxsize=8)
def _index(root: str, stamp: int) -> tuple[dict[str, list[Path]], dict[str, list[Path]], list[Path]]:
    """package name -> files, module name -> files, all HDL files below root."""
    pkgs: dict[str, list[Path]] = {}
    mods: dict[str, list[Path]] = {}
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # other git repositories inside the project (tool sources, ...) are not part of the design
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP and not d.startswith(".")
                             and not (Path(dirpath) / d / ".git").exists())
        for fn in sorted(filenames):
            if fn.endswith(HDL):
                files.append(Path(dirpath) / fn)
        if len(files) > MAX_INDEX_FILES:
            log.warn(f"More than {MAX_INDEX_FILES} HDL files below {root}: the search for missing files stops there")
            break
    for f in files:
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        dp, dm, _, _, _ = scan(f)
        for n in dp:
            pkgs.setdefault(n, []).append(f)
        for n in dm:
            mods.setdefault(n, []).append(f)
    return pkgs, mods, files


def _closest(cands: list[Path], near: Path) -> Path:
    """The candidate sharing the longest directory prefix with `near` (then the shortest path)."""
    def key(c: Path):
        common = len(os.path.commonpath([str(c.parent), str(near.parent)]))
        return (-common, len(c.parts), str(c))
    return sorted(cands, key=key)[0]


def resolve(files: list[Path], incdirs: list[Path],
            root: Path | None = None) -> tuple[list[Path], list[Path], list[str]]:
    """Files and include folders to add for what `files` use but do not define; plus notes for the log."""
    files = [Path(f).resolve() for f in files]
    have = set(files)
    defined_p: set[str] = set()
    defined_m: set[str] = set()
    for f in files:
        dp, dm, _, _, _ = scan(f)
        defined_p |= dp
        defined_m |= dm
    root = root or project_root(files)
    added: list[Path] = []
    add_inc: list[Path] = []
    notes: list[str] = []
    index = None

    def idx():
        nonlocal index
        if index is None:
            index = _index(str(root), 0) if root else ({}, {}, [])
        return index

    queue = list(files)
    inc_dirs = [Path(d).resolve() for d in incdirs]
    while queue and len(added) < MAX_ADDED:
        f = queue.pop(0)
        _, _, up, um, incs = scan(f)
        for inc in incs:   # an include that is not found next to the file or in an include folder
            if any((d / inc).is_file() for d in [f.parent, *inc_dirs]) or not root:
                continue
            hits = [c for c in idx()[2] if c.as_posix().endswith("/" + inc.lstrip("./"))]
            if hits:
                hit = _closest(hits, f)
                d = Path(str(hit)[: -len(inc)].rstrip("/\\") or "/")
                if d not in inc_dirs:
                    inc_dirs.append(d)
                    add_inc.append(d)
                    notes.append(f"include folder {_rel(d, root)} (for `include \"{inc}\")")
        for name in sorted(up - defined_p):
            if not root or name not in idx()[0]:
                continue
            hit = _closest(idx()[0][name], f)
            defined_p.add(name)
            if hit not in have:
                have.add(hit)
                added.append(hit)
                queue.append(hit)
                notes.append(f"{_rel(hit, root)} (package {name})")
        for name in sorted(um - defined_m - defined_p):
            if not root or name not in idx()[1]:
                continue
            hit = _closest(idx()[1][name], f)
            if hit not in have:
                have.add(hit)
                added.append(hit)
                queue.append(hit)
                defined_m |= scan(hit)[1]
                defined_p |= scan(hit)[0]
                notes.append(f"{_rel(hit, root)} (module {name})")
            defined_m.add(name)
    return added, add_inc, notes


def _rel(p: Path, root: Path | None) -> str:
    try:
        return p.relative_to(root).as_posix() if root else str(p)
    except ValueError:
        return str(p)


def order_packages_first(files: list[Path]) -> list[Path]:
    """Files defining packages first (in dependency order), so frontends that read files one by
    one (Yosys' own parser) see a package before its users."""
    pk = [f for f in files if scan(f)[0]]
    rest = [f for f in files if not scan(f)[0]]
    defs = {n: f for f in pk for n in scan(f)[0]}
    out: list[Path] = []
    seen: set[Path] = set()

    def visit(f: Path, depth: int = 0) -> None:
        if f in seen or depth > 50:
            return
        seen.add(f)
        for n in sorted(scan(f)[2]):
            if n in defs and defs[n] != f:
                visit(defs[n], depth + 1)
        out.append(f)
    for f in pk:
        visit(f)
    return out + rest
