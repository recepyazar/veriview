"""Source collection and EDA file list (.f) parsing."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import log
from .log import VeriViewError

SV_SUFFIXES = (".sv", ".svh")

_ENV_RE = re.compile(r"\$(?:\{(\w+)\}|\((\w+)\)|(\w+))")
_DEFINE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*(=[^\s"]*)?$')


@dataclass
class SourceSet:
    files: list[Path] = field(default_factory=list)
    incdirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)

    @property
    def has_systemverilog(self) -> bool:
        return any(f.suffix in SV_SUFFIXES for f in self.files)

    def validate(self) -> None:
        if not self.files:
            raise VeriViewError("No source files found.")
        for f in self.files:
            if not f.is_file():
                raise VeriViewError(f"Source file not found: {f}")
        for d in self.incdirs:
            if not d.is_dir():
                log.warn(f"Include directory not found: {d}")
        for d in self.defines:
            if not _DEFINE_RE.match(d):
                raise VeriViewError(f"Invalid define (no spaces/quotes allowed): '{d}'")


def expand_env(text: str) -> str:
    """Expand $VAR, ${VAR} and $(VAR); unset variables expand to ''."""
    return _ENV_RE.sub(lambda m: os.environ.get(next(g for g in m.groups() if g), ""), text)


def _resolve(base: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base / p


def _strip_comment(line: str) -> str:
    idx = line.find("//")
    return line[:idx] if idx >= 0 else line


def parse_filelist(path: Path, out: SourceSet, _seen: set[Path] | None = None) -> SourceSet:
    """Parse a .f file list into `out`. Relative paths resolve against the .f's directory."""
    seen = _seen if _seen is not None else set()
    if not path.is_file():
        raise VeriViewError(f"File list not found: {path}")
    key = path.resolve()
    if key in seen:
        raise VeriViewError(f"Recursive file list include: {path}")
    seen.add(key)
    base = path.parent
    log.info(f"Reading file list: {path}")

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = expand_env(_strip_comment(raw)).strip()
        if not line:
            continue

        # A plain source line is a single path (spaces allowed).
        if not line.startswith(("+", "-")):
            out.files.append(_resolve(base, line))
            continue

        tokens = iter(shlex.split(line))
        for tok in tokens:

            def next_value(opt: str = tok, it: Iterator[str] = tokens) -> str:
                value = next(it, None)
                if value is None:
                    raise VeriViewError(f"{path}: option '{opt}' requires a value")
                return value

            if tok.startswith("+incdir+"):
                out.incdirs += [_resolve(base, d) for d in tok[len("+incdir+"):].split("+") if d]
            elif tok.startswith("+define+"):
                out.defines += [d for d in tok[len("+define+"):].split("+") if d]
            elif tok in ("-f", "-F"):
                parse_filelist(_resolve(base, next_value()), out, seen)
            elif tok == "-v":
                out.files.append(_resolve(base, next_value()))
            elif tok == "-I":
                out.incdirs.append(_resolve(base, next_value()))
            elif tok.startswith("-I"):
                out.incdirs.append(_resolve(base, tok[2:]))
            elif tok == "-D":
                out.defines.append(next_value())
            elif tok.startswith("-D"):
                out.defines.append(tok[2:])
            elif tok == "-y":
                next_value()
                log.warn("Ignoring unsupported file list option: -y")
            elif tok.startswith(("+", "-")):
                log.warn(f"Ignoring unsupported file list option: {tok}")
            else:
                out.files.append(_resolve(base, tok))
    return out


def collect_sources(inputs: list[str], incdirs: list[str], defines: list[str]) -> SourceSet:
    out = SourceSet(incdirs=[Path(d) for d in incdirs], defines=list(defines))
    for item in inputs:
        p = Path(item)
        if p.suffix == ".f":
            parse_filelist(p, out)
        else:
            out.files.append(p)
    out.validate()
    return out
