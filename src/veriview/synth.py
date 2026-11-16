"""Yosys synthesis with pluggable SystemVerilog frontends (yosys, slang, sv2v)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, TypeVar

from . import log
from .deps import project_root
from .filelist import SourceSet
from .log import VeriViewError
from .tools import find_tool, project_yosys_with_slang, slang_available, slang_has_option, slang_mode

FRONTENDS = ("auto", "yosys", "sv2v", "slang")
# Errors caused by the design/invocation itself rather than parser support.
_DESIGN_ERROR_RE = re.compile(r"Module `[^']*' not found|Can't open include file|Yosys was stopped by signal")
# where an error happened: yosys-slang's "Source line f.sv:12:5:", slang's "f.sv:12:5: error:"
# (older yosys-slang versions print "f.sv:12: ERROR: ...")
_ERR_LOC_RE = re.compile(r"^Source line (\S+?):(\d+)(?::\d+)?:|^(\S+?):(\d+)(?::\d+)?: (?:error|ERROR):", re.M)
_MODULE_RE = re.compile(r"^[ \t]*(?:macromodule|module)\s+(?:automatic\s+|static\s+)?(\w+)|^[ \t]*(endmodule)\b", re.M)
MAX_BLACKBOXES = 8


def summarize_error(text: str, max_lines: int = 25, per_kind: int = 3) -> str:
    """The lines of a tool's error output that say something (yosys-slang dumps whole ASTs);
    repeated lines dropped, at most `per_kind` lines with the same message (at other places)."""
    lines = list(dict.fromkeys(ln.rstrip() for ln in text.splitlines()))
    keep = [ln for ln in lines if re.search(r"error|Source line|unimplemented|not found|failed", ln, re.I)]
    if not keep:
        keep = [ln for ln in lines if ln.strip() and ln.strip() not in ("{", "}", "[", "]", "},", "],")]
    out: list[str] = []
    kinds: dict[str, int] = {}
    for ln in keep:
        kind = re.sub(r"^\S+?:\d+(?::\d+)?: ", "", ln)   # the message without its location
        kinds[kind] = kinds.get(kind, 0) + 1
        if kinds[kind] <= per_kind:
            out.append(ln)
    more = {k: n - per_kind for k, n in kinds.items() if n > per_kind}
    for k, n in more.items():
        idx = max(i for i, ln in enumerate(out) if ln.endswith(k))
        out.insert(idx + 1, f"  ... and {n} more like this")
    if len(out) > max_lines:
        out = out[:max_lines] + [f"... ({len(out) - max_lines} more lines)"]
    return "\n".join(out)


TOP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

T = TypeVar("T")


class FrontendError(Exception):
    """A frontend could not read/synthesize the design; message is the tool output."""


def yosys_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yosys_path_arg(s: str) -> str:
    """Path for option values: older Yosys (e.g. 0.33) does not unquote them, so
    quote only when needed (paths with whitespace)."""
    return yosys_quote(s) if re.search(r"\s", s) else s


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
    if stage == "fsm":  # internal: FSM extraction for diagrams (see fsm.py)
        from .fsm import FSM_STAGE_COMMANDS
        return list(FSM_STAGE_COMMANDS)
    if stage == "editor":  # internal: gate-editor primitives ("open in editor", see to_editor.py)
        from .to_editor import editor_commands
        return editor_commands()
    if stage == "editor-blocks":  # internal: the editor's blocks + gates for the rest (to_editor.py)
        from .to_editor import blocks_commands
        return blocks_commands()
    cmds = ["proc"]
    if flatten:
        cmds.append("flatten")
    if stage == "elab":
        # plain opt_clean (no -purge): keep the names of intermediate nets as written
        # memory_collect: each memory as one $mem_v2 cell (one box in the schematic; simulated)
        cmds += ["memory_collect", "setattr -set keep 1 */c:* */t:$scopeinfo %d", "opt_clean"]
    elif stage == "opt":
        cmds += ["opt", "memory_collect", "opt_clean -purge"]
    elif stage == "gate":
        # -noshare: SAT-based resource sharing only saves area and takes ages on big designs
        # memories of 2 kbit and more stay memory macros ($mem_v2, like SRAMs in a real flow):
        # mapped to flip-flops, a cache becomes hundreds of thousands of cells (and Yosys runs out
        # of memory); select cannot multiply SIZE * WIDTH, hence the pairs of thresholds
        macro = [f"setattr -set veriview_macro 1 t:$mem_v2 r:SIZE>={n} %i r:WIDTH>={w} %i"
                 for n, w in ((2048, 1), (1024, 2), (512, 4), (256, 8), (128, 16), (64, 32), (32, 64), (16, 128),
                              (8, 256), (4, 512))]
        cmds += ["synth -noshare -run coarse:fine", *macro, "memory_map -attr !veriview_macro", "techmap",
                 "opt -fast", f"abc -g {GATE_SET}", "opt_clean -purge"]
    else:
        raise ValueError(stage)
    return cmds


_ANNOUNCED: set[str] = set()


class Synthesizer:
    def __init__(self, sources: SourceSet, work_dir: Path, frontend: str = "auto", flatten: bool = False,
                 stage: str = "opt"):
        self.sources = sources
        self.work_dir = work_dir
        self.frontend = frontend
        self.flatten = flatten
        self.stage = stage
        self.yosys = os.environ.get("VERIVIEW_YOSYS") or find_tool("yosys") or "yosys"
        self._sv2v_out: Path | None = None
        self._incdir_links: list[str] | None = None
        self.selected: str | None = None
        self.last_error = ""
        self.errors: dict[str, str] = {}
        self.blackbox: list[str] = []      # modules slang imports as black boxes (could not be elaborated)
        self._top: str | None = None
        self.unroll_limit: int | None = None   # read_slang --unroll-limit, raised when a loop needs it

    # --- frontend plumbing ----------------------------------------------------

    def candidates(self) -> list[str]:
        if self.frontend != "auto":
            return [self.frontend]
        cands = ["yosys"]
        if self.sources.has_systemverilog:
            if not slang_available(self.yosys) and not os.environ.get("VERIVIEW_YOSYS"):
                root = project_root(self.sources.files)
                local = project_yosys_with_slang(str(root)) if root else None
                if local:
                    if local not in _ANNOUNCED:
                        _ANNOUNCED.add(local)
                        log.info(f"Using the project's Yosys with slang: {local}")
                    self.yosys = local
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

    def _slang_path(self, i: int, f: Path) -> str:
        # read_slang takes its file names literally (quotes are not removed): paths with
        # spaces are passed through space-free symlinks in the work dir
        if not re.search(r"\s", str(f)):
            return str(f)
        link = self.work_dir / f"src{i}{f.suffix}"
        if not link.exists():
            try:
                os.symlink(f.resolve(), link)
            except OSError:
                shutil.copyfile(f, link)
        return str(link)

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
            files = " ".join(self._slang_path(i, f) for i, f in enumerate(self.sources.files))
            load = ["plugin -i slang"] if slang_mode(self.yosys) == "plugin" else []
            # --keep-hierarchy: slang otherwise flattens the whole design into the top module
            bb = "".join(f" --blackboxed-module {m}" for m in self.blackbox)
            if self.unroll_limit:
                bb += f" --unroll-limit={self.unroll_limit}"
            return [*load, f"read_slang --keep-hierarchy{opts}{bb}{f' --top {top}' if top else ''} {files}"]
        raise ValueError(fe)

    def _yosys(self, fe: str, script: list[str], name: str) -> None:
        ys = self.work_dir / name
        ys.write_text("\n".join(script) + "\n")
        cwd = self.work_dir if fe == "sv2v" else None
        log_file = ys.with_suffix(".log")
        r = subprocess.run([self.yosys, "-q", "-l", str(log_file.resolve()), "-s", str(ys.resolve())],
                           capture_output=True, text=True, cwd=cwd)
        if r.returncode < 0:   # killed (the out-of-memory killer: SIGKILL) - another frontend will not help
            sig = -r.returncode
            raise FrontendError(f"ERROR: Yosys was stopped by signal {sig}"
                                + (" (most likely out of memory: try a smaller top module, or fewer views)"
                                   if sig == 9 else "") + f" during {name.rsplit('.', 1)[0].replace('synth_', '')}")
        if r.returncode != 0:
            # with -q, diagnostics (e.g. slang's "f.sv:12:5: error: ...") only reach the log file
            try:
                diag = [ln for ln in log_file.read_text(errors="replace").splitlines()
                        if re.search(r"^\S+:\d+(?::\d+)?: (?:error|ERROR): ", ln)]
            except OSError:
                diag = []
            out = (r.stdout + r.stderr).strip()
            raise FrontendError(summarize_error("\n".join([*dict.fromkeys(diag), out])))

    def _module_at(self, file: str, line: int) -> str | None:
        """The module whose text contains file:line (file as named in an error message)."""
        cands = [f for f in self.sources.files if str(f) == file or f.as_posix().endswith("/" + file.lstrip("./"))
                 or f.resolve() == Path(file).resolve()]
        if not cands:
            return None
        try:
            text = cands[0].read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        pos = sum(len(x) + 1 for x in text.split("\n")[: line - 1])
        current = None
        for m in _MODULE_RE.finditer(text):
            if m.start() > pos:
                break
            current = None if m.group(2) else m.group(1)
        return current

    def _roots(self) -> set[str]:
        """The top module, or (no top given) the modules no other module instantiates."""
        if self._top:
            return {self._top}
        from .deps import scan
        defined, used = set(), set()
        for f in self.sources.files:
            sc = scan(f)
            defined |= sc[1]
            used |= sc[3]
        return defined - used

    def _run(self, fe: str, action: Callable[[str], T]) -> T:
        """action(fe); with slang, modules that cannot be elaborated for synthesis (e.g. simulation
        models calling DPI functions) are retried as black boxes, so the rest of the design shows."""
        while True:
            try:
                return action(fe)
            except FrontendError as e:
                if (fe != "slang" or len(self.blackbox) >= MAX_BLACKBOXES
                        or not slang_has_option(self.yosys, "--blackboxed-module")):
                    raise
                err = str(e)
                # long generate / for loops (cache arrays, tables): a higher unroll limit, not a black box
                if "unroll limit" in err and (self.unroll_limit or 0) < 1_000_000:
                    self.unroll_limit = 1_000_000
                    log.info("A loop needs more than slang's default unroll limit: retrying with a higher one")
                    continue
                roots = self._roots()
                mod = where = why = None
                for m in _ERR_LOC_RE.finditer(err):
                    f, ln = m.group(1) or m.group(3), int(m.group(2) or m.group(4))
                    cand = self._module_at(f, ln)
                    if cand and cand not in roots and cand not in self.blackbox:
                        line = err[m.start():err.find("\n", m.start()) if "\n" in err[m.start():] else len(err)]
                        if m.group(1):   # yosys-slang's "Source line": the reason is the ERROR line after it
                            line = next((x for x in err[m.start():].splitlines() if "ERROR" in x), line)
                        mod, where, why = cand, f"{f}:{ln}", line.strip()[:160]
                        break
                if not mod:
                    raise
                log.warn(f"Module {mod} cannot be synthesized (at {where}: {why}); it is shown as a black box")
                self.blackbox.append(mod)

    def _with_frontend(self, action: Callable[[str], T]) -> T:
        if self.selected:
            try:
                return self._run(self.selected, action)
            except FrontendError as e:
                raise VeriViewError(f"Synthesis failed.\n{e}") from e
        self.errors: dict[str, str] = {}
        for fe in self.candidates():
            try:
                result = self._run(fe, action)
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

    def _simulation_only_hint(self) -> str:
        """For a top module that only works in simulation ($value$plusargs, DPI, ...): its sub-modules."""
        err = self.errors.get("slang", "")
        if not re.search(r"unsupported system task|DPI|Feature unimplemented", err):
            return ""
        from .deps import scan
        tops = []
        for m in _ERR_LOC_RE.finditer(err):
            mod = self._module_at(m.group(1) or m.group(3), int(m.group(2) or m.group(4)))
            if mod and mod not in self.blackbox and mod not in tops:
                tops.append(mod)
        if not tops:
            return ""
        defined = {n for f in self.sources.files for n in scan(f)[1]}
        subs: list[str] = []
        for f in self.sources.files:
            if tops[0] in scan(f)[1]:
                subs = sorted(n for n in scan(f)[3] & defined if n != tops[0] and n not in self.blackbox)
        task = re.search(r"unsupported system task '([^']+)'", err)
        what = f"the system task {task.group(1)}" if task else "code slang cannot synthesize"
        hint = f"\n{tops[0]} uses {what}, which only works in simulation: it cannot be the top module of a schematic."
        if subs:
            hint += f"\nTry a synthesizable top module: {', '.join(subs)}"
        return hint

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
        hint = self._simulation_only_hint()
        if hint:   # the explanation first: the tools' messages follow
            msg = "Synthesis failed." + hint + msg[len("Synthesis failed."):]
        return VeriViewError(msg)

    # --- public API -----------------------------------------------------------

    def synthesize(self, top: str | None, json_out: Path, stage: str | None = None,
                   subst: dict[str, str] | None = None) -> None:
        """subst: extra {placeholder} replacements for the stage commands (see fsm.py)."""
        self._top = top
        def action(fe: str) -> None:
            script = self._read_commands(fe, top)
            script.append(f"hierarchy -check -top {top}" if top else "hierarchy -check -auto-top")
            repl = {"{pre_json}": yosys_quote(str(json_out.with_suffix(".pre.json"))),
                    "{detect_log}": yosys_path_arg(str(json_out.with_suffix(".detect.log"))), "{force}": ""}
            repl.update(subst or {})
            for c in stage_commands(stage or self.stage, self.flatten):
                for k, v in repl.items():
                    c = c.replace(k, v)
                script.append(c)
            script.append(f"write_json {yosys_quote(str(json_out))}")
            self._yosys(fe, script, f"synth_{stage or self.stage}.ys")

        self._with_frontend(action)

    def module_ports(self, top: str) -> list[dict[str, Any]]:
        """Ports of a module (default parameters): [{name, dir, bits}] in declaration order."""
        out = self.work_dir / "ports.json"

        def action(fe: str) -> list[dict[str, Any]]:
            script = [*self._read_commands(fe, top), f"hierarchy -top {top}", "proc",
                      f"write_json {yosys_quote(str(out))}"]
            self._yosys(fe, script, "ports.ys")
            mods = json.loads(out.read_text())["modules"]
            name = next((n for n, m in mods.items() if str(m.get("attributes", {}).get("top", "")).strip("0")), top)
            return [{"name": n, "dir": p.get("direction", "input"), "bits": len(p.get("bits", []))}
                    for n, p in mods[name].get("ports", {}).items()]
        self._top = top
        return self._with_frontend(action)

    def top_candidates(self) -> list[str]:
        """Modules that no other module instantiates (possible top modules), sorted."""
        out = self.work_dir / "candidates.json"

        def base(n: str) -> str:
            if n.startswith(("$paramod", "$abstract")):
                parts = n.split("\\")
                return parts[1] if len(parts) > 1 else n
            return n.lstrip("\\")

        def action(fe: str) -> list[str]:
            script = self._read_commands(fe, None) + ["hierarchy", "proc", f"write_json {yosys_quote(str(out))}"]
            self._yosys(fe, script, "candidates.ys")
            mods = json.loads(out.read_text()).get("modules", {})
            used = {base(c["type"]) for m in mods.values() for c in m.get("cells", {}).values()
                    if not c["type"].startswith("$") or c["type"].startswith("$paramod")}
            return sorted({base(n) for n in mods} - used)

        return self._with_frontend(action)

    def list_modules(self) -> list[str]:
        out = self.work_dir / "modules.txt"

        def action(fe: str) -> list[str]:
            script = self._read_commands(fe, None) + [f"tee -q -o {yosys_path_arg(str(out))} ls"]
            self._yosys(fe, script, "list.ys")
            names = set()
            for line in out.read_text().splitlines():
                m = re.match(r"^  (?:\$abstract\\)?(\S+)$", line)
                if m:
                    names.add(m.group(1))
            return sorted(names)

        return self._with_frontend(action)
