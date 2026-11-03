"""Yosys synthesis with pluggable SystemVerilog frontends (yosys, slang, sv2v)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable, TypeVar

from . import log
from .filelist import SourceSet
from .log import VeriViewError
from .tools import find_tool, slang_available

FRONTENDS = ("auto", "yosys", "sv2v", "slang")
# Errors caused by the design/invocation itself rather than parser support.
_DESIGN_ERROR_RE = re.compile(r"Module `[^']*' not found|Can't open include file")
TOP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

T = TypeVar("T")


class FrontendError(Exception):
    """A frontend could not read/synthesize the design; message is the tool output."""


def yosys_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# sv2v lowers elaboration-time $error/$warning to `initial $display(...)`, which
# Yosys ignores. Turn them back so parameter checks still stop synthesis.
_SV2V_ELAB = [
    (re.compile(r'initial \$display\("Error \[elaboration\][^"]*", (.*)\);$', re.M), r"$error(\1);"),
    (re.compile(r'initial \$display\("Warning \[elaboration\][^"]*", (.*)\);$', re.M), r"$warning(\1);"),
]


def fix_sv2v_output(text: str) -> str:
    for pattern, repl in _SV2V_ELAB:
        text = pattern.sub(repl, text)
    return text


STAGES = ("elab", "opt", "gate")
# Gate set for --stage gate: all of these have netlistsvg symbols.
GATE_SET = "AND,NAND,OR,NOR,XOR,XNOR,MUX"


def stage_commands(stage: str, flatten: bool) -> list[str]:
    """Yosys commands after `hierarchy` for the given view stage.

    elab: elaborated RTL as written; every cell gets `keep` so nothing is
          optimized away or merged (unused instances/logic stay visible).
    opt:  coarse-grain optimized RTL (unused logic removed, duplicates merged).
    gate: full synthesis to simple gates and flip-flops.
    """
    cmds = ["proc"]
    if flatten:
        cmds.append("flatten")
    if stage == "elab":
        cmds += ["setattr -set keep 1 */c:* */t:$scopeinfo %d", "opt_clean -purge"]
    elif stage == "opt":
        cmds += ["opt", "opt_clean -purge"]
    elif stage == "gate":
        cmds += ["synth -run coarse:fine", "memory_map", "techmap", "opt -fast",
                 f"abc -g {GATE_SET}", "opt_clean -purge"]
    else:
        raise ValueError(stage)
    return cmds


class Synthesizer:
    def __init__(self, sources: SourceSet, work_dir: Path, frontend: str = "auto", flatten: bool = False,
                 stage: str = "opt"):
        self.sources = sources
        self.work_dir = work_dir
        self.frontend = frontend
        self.flatten = flatten
        self.stage = stage
        self.yosys = find_tool("yosys") or "yosys"
        self._sv2v_out: Path | None = None
        self._incdir_links: list[str] | None = None
        self.selected: str | None = None
        self.last_error = ""
        self.errors: dict[str, str] = {}

    # --- frontend plumbing ----------------------------------------------------

    def candidates(self) -> list[str]:
        if self.frontend != "auto":
            return [self.frontend]
        cands = ["yosys"]
        if self.sources.has_systemverilog:
            if slang_available(self.yosys):
                cands.append("slang")
            if find_tool("sv2v"):
                cands.append("sv2v")
        return cands

    def _incdirs(self) -> list[str]:
        # Yosys does not unquote option values, so include dirs are passed via
        # space-free symlinks inside the work dir.
        if self._incdir_links is None:
            self._incdir_links = []
            for i, d in enumerate(self.sources.incdirs):
                link = self.work_dir / f"inc{i}"
                try:
                    os.symlink(d.resolve(), link, target_is_directory=True)
                    self._incdir_links.append(str(link))
                except OSError:  # e.g. Windows without symlink rights
                    self._incdir_links.append(str(d))
        return self._incdir_links

    def _run_sv2v(self) -> Path:
        if self._sv2v_out is not None:
            return self._sv2v_out
        sv2v = find_tool("sv2v")
        if not sv2v:
            raise FrontendError("sv2v is not installed (run with --install-deps)")
        cmd = [sv2v]
        for d in self.sources.incdirs:
            cmd += ["-I", str(d)]
        for d in self.sources.defines:
            cmd += ["-D", d]
        cmd += [str(f) for f in self.sources.files]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise FrontendError(r.stderr.strip() or r.stdout.strip())
        # Named after the first source and read via a relative path (Yosys runs in
        # work_dir), so cell names in the SVG don't contain the random temp dir.
        out = self.work_dir / f"{self.sources.files[0].stem}.sv2v.v"
        out.write_text(fix_sv2v_output(r.stdout))
        self._sv2v_out = out
        return out

    def _read_commands(self, fe: str, top: str | None) -> list[str]:
        opts = "".join(f" -I {d}" for d in self._incdirs()) + "".join(f" -D {d}" for d in self.sources.defines)
        if fe == "yosys":
            return [
                f"read_verilog{' -sv' if f.suffix in ('.sv', '.svh') else ''}{opts} {yosys_quote(str(f))}"
                for f in self.sources.files
            ]
        if fe == "sv2v":
            return [f"read_verilog {yosys_quote(self._run_sv2v().name)}"]
        if fe == "slang":
            files = " ".join(yosys_quote(str(f)) for f in self.sources.files)
            return ["plugin -i slang", f"read_slang{opts}{f' --top {top}' if top else ''} {files}"]
        raise ValueError(fe)

    def _yosys(self, fe: str, script: list[str], name: str) -> None:
        ys = self.work_dir / name
        ys.write_text("\n".join(script) + "\n")
        cwd = self.work_dir if fe == "sv2v" else None
        r = subprocess.run([self.yosys, "-q", "-s", str(ys.resolve())], capture_output=True, text=True, cwd=cwd)
        if r.returncode != 0:
            raise FrontendError((r.stdout + r.stderr).strip())

    def _with_frontend(self, action: Callable[[str], T]) -> T:
        if self.selected:
            try:
                return action(self.selected)
            except FrontendError as e:
                raise VeriViewError(f"Synthesis failed.\n{e}") from e
        self.errors: dict[str, str] = {}
        for fe in self.candidates():
            try:
                result = action(fe)
            except FrontendError as e:
                self.last_error = str(e)
                self.errors[fe] = self.last_error
                if _DESIGN_ERROR_RE.search(self.last_error):
                    break  # not a parser limitation; another frontend won't help
                first = self.last_error.splitlines()[0] if self.last_error else ""
                log.info(f"Frontend '{fe}' failed ({first}); trying the next one")
                continue
            self.selected = fe
            if fe != "yosys" or self.frontend != "auto":
                log.info(f"Frontend: {fe}")
            return result
        raise self._failure()

    def _failure(self) -> VeriViewError:
        msg = "Synthesis failed."
        errors = self.errors
        if len(errors) > 1:
            # Yosys' own message first: it is usually the most familiar one
            msg += "".join(f"\n[{fe}] {err}" for fe, err in errors.items())
        elif self.last_error:
            msg += "\n" + self.last_error
        if (
            self.frontend == "auto"
            and self.sources.has_systemverilog
            and "syntax error" in errors.get("yosys", self.last_error)
            and not find_tool("sv2v")
            and not slang_available(self.yosys)
        ):
            msg += (
                "\nYosys' built-in parser supports only part of SystemVerilog (e.g. no unpacked array ports)."
                "\nInstall sv2v for full support: veriview --install-deps"
            )
        return VeriViewError(msg)

    # --- public API -----------------------------------------------------------

    def synthesize(self, top: str | None, json_out: Path, stage: str | None = None) -> None:
        def action(fe: str) -> None:
            script = self._read_commands(fe, top)
            script.append(f"hierarchy -check -top {top}" if top else "hierarchy -check -auto-top")
            script += stage_commands(stage or self.stage, self.flatten)
            script.append(f"write_json {yosys_quote(str(json_out))}")
            self._yosys(fe, script, f"synth_{stage or self.stage}.ys")

        self._with_frontend(action)

    def list_modules(self) -> list[str]:
        out = self.work_dir / "modules.txt"

        def action(fe: str) -> list[str]:
            script = self._read_commands(fe, None) + [f"tee -q -o {yosys_quote(str(out))} ls"]
            self._yosys(fe, script, "list.ys")
            names = set()
            for line in out.read_text().splitlines():
                m = re.match(r"^  (?:\$abstract\\)?(\S+)$", line)
                if m:
                    names.add(m.group(1))
            return sorted(names)

        return self._with_frontend(action)
