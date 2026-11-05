"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path

from . import __version__, log
from .filelist import collect_sources
from .html import build_viewer
from .log import VeriViewError
from .render import open_path, render
from .scope import find_top, postprocess, reroot, resolve_instance
from .synth import FRONTENDS, STAGES, TOP_RE, Synthesizer
from .tools import ensure_dependencies, warn_if_old_yosys
from .watch import BuildResult, watch

HDL_SUFFIXES = (".v", ".sv", ".svh", ".vh", ".f")

DESCRIPTION = """\
Interactive schematics, simulation, timing and FSM diagrams for Verilog/SystemVerilog,
built on Yosys and netlistsvg.

quick start:
  veriview design.sv                 open the interactive viewer (writes design.html)
  veriview rtl/                      a whole folder (uses its .f file, or all .v/.sv files)
  veriview design.f --watch          live: the browser reloads on every save
  veriview design.sv -o design.svg   a plain SVG schematic instead
"""

EPILOG = """\
more examples:
  veriview design.f --timing 2ns     gate-level timing report (setup/hold, fmax, area)
  veriview design.f --fsm            list the state machines
  veriview design.f -o x.svg --select u_core/u_alu --depth 1
  veriview design.f -o x.svg --flatten --cone o_valid

file list (.f) syntax:
  one source per line, // and # comments, +incdir+<dir>[+<dir>...],
  +define+<N>[=<V>][+...], -f/-F <nested.f>, -v <lib file>, -I<dir>, -D<N>,
  $VAR / ${VAR} / $(VAR) environment variables.
  Relative paths are resolved against the directory of the .f file.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veriview",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="*", metavar="input",
                   help=".v/.sv files, .f file lists or a folder (a trailing non-file word is the top module)")
    p.add_argument("-t", "--top", help="top module (default: auto-detected)")
    p.add_argument("-o", "--output", help="output file: name.html (viewer, default) or name.svg (schematic); "
                                         "with --split: a directory")
    p.add_argument("-f", "-F", dest="filelists", action="append", default=[], metavar="FILE.f",
                   help="read a file list (repeatable)")
    p.add_argument("-I", "--incdir", dest="incdirs", action="append", default=[], metavar="DIR",
                   help="include directory (repeatable)")
    p.add_argument("-D", "--define", dest="defines", action="append", default=[], metavar="N[=V]",
                   help="preprocessor define (repeatable)")
    p.add_argument("--stage", choices=STAGES, default="opt",
                   help="what to draw: elab = RTL as written (nothing removed), opt = optimized RTL (default), "
                        "gate = synthesized gates and flip-flops")
    p.add_argument("--flatten", action="store_true", help="flatten hierarchy (expand sub-modules)")
    p.add_argument("--select", metavar="INST/PATH",
                   help="draw only this sub-instance (e.g. u_core/u_alu), with its actual parameters")
    p.add_argument("--depth", type=int, metavar="N",
                   help="expand N levels of hierarchy below the top; deeper sub-modules stay boxes")
    p.add_argument("--cone", action="append", default=[], metavar="SIGNAL",
                   help="draw only the logic driving SIGNAL, stopping at registers (repeatable)")
    p.add_argument("--split", action="store_true", help="render every module to its own SVG")
    p.add_argument("--svg", action="store_true", help="write a plain SVG schematic instead of the HTML viewer")
    p.add_argument("--html", action="store_true",
                   help="write an interactive HTML viewer (hierarchy navigation, stage/flatten switches, "
                        "cone highlighting)")
    p.add_argument("--timing", metavar="PERIOD",
                   help="print a static timing report of the gate-level netlist for this clock period "
                        "(e.g. 2ns, 500ps, 250MHz) instead of drawing")
    p.add_argument("--input-delay", default="100ps", metavar="T", help="--timing: input arrival after the clock edge")
    p.add_argument("--output-delay", default="0ps", metavar="T", help="--timing: required output setup before the edge")
    p.add_argument("--uncertainty", default="0ps", metavar="T", help="--timing: clock uncertainty (skew/jitter)")
    p.add_argument("--fsm", action="store_true",
                   help="print the state machines found in the design (states, encodings, transitions); "
                        "with -o file.dot write a Graphviz diagram")
    p.add_argument("--html-stages", default=",".join(STAGES), metavar="LIST",
                   help="stages embedded in the HTML viewer (default: elab,opt,gate; --stage is shown first)")
    p.add_argument("--frontend", choices=FRONTENDS, default="auto",
                   help="SystemVerilog frontend (default: auto = yosys, falling back to slang/sv2v)")
    p.add_argument("--skin", type=Path, help="custom netlistsvg skin")
    p.add_argument("--bg", default="white", metavar="COLOR|none", help="background color (default: white)")
    p.add_argument("--keep-json", action="store_true", help="keep the Yosys JSON netlist next to the SVG")
    p.add_argument("--open", action=argparse.BooleanOptionalAction, default=None,
                   help="open the result (default: the viewer opens when no -o is given in a terminal)")
    p.add_argument("--watch", action="store_true",
                   help="rebuild whenever a source file changes; with --html, serve the viewer on a local "
                        "web server that reloads automatically")
    p.add_argument("--port", type=int, default=8765, help="port for --watch --html (default: 8765)")
    p.add_argument("--install-deps", action="store_true",
                   help="install missing dependencies (yosys, node, netlistsvg, sv2v)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation when installing")
    p.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    p.add_argument("-V", "--version", action="version", version=f"VeriView {__version__}")
    return p


def _split_positionals(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    """Separate source files from the legacy positional top-module name."""
    inputs: list[str] = []
    for item in args.inputs:
        if Path(item).exists() or item.endswith(HDL_SUFFIXES):
            inputs.append(item)
        elif args.top is None:
            args.top = item
        else:
            parser.error(f"unexpected argument '{item}' (top module already set to '{args.top}')")
    return inputs + args.filelists


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}


def _expand_dirs(inputs: list[str]) -> tuple[list[str], str | None]:
    """A folder becomes its .f file list (if it has exactly one) or all .v/.sv files in it."""
    out: list[str] = []
    stem = None
    for item in inputs:
        d = Path(item)
        if not d.is_dir():
            out.append(item)
            continue
        stem = stem or d.resolve().name
        lists = sorted(d.glob("*.f"))
        if len(lists) == 1:
            log.info(f"Using file list {lists[0]}")
            out.append(str(lists[0]))
            continue
        if len(lists) > 1:
            raise VeriViewError(f"{item} contains several file lists ({', '.join(p.name for p in lists)}); "
                                "pass the one you want.")
        files = sorted(str(f) for f in d.rglob("*")
                       if f.suffix in (".v", ".sv") and f.is_file()
                       and not any(part in SKIP_DIRS or part.startswith(".") for part in f.relative_to(d).parts[:-1]))
        if not files:
            raise VeriViewError(f"No .v/.sv/.f files found in {item}")
        log.info(f"Using {len(files)} source file(s) from {item}")
        out += files
    return out, stem


def _output_path(args: argparse.Namespace, inputs: list[str], suffix: str, stem: str | None = None) -> Path:
    stem = stem or args.top or getattr(args, "stem_hint", None) or Path(inputs[0]).stem
    if args.output:
        out = Path(args.output)
        if args.output.endswith(("/", "\\")) or out.is_dir():
            return out / f"{stem}{suffix}"
        return out
    return Path(f"{stem}{suffix}")


def _run_html(args: argparse.Namespace, inputs: list[str], synth: Synthesizer, work: Path,
              stages: list[str]) -> Path:
    stage_netlists = {}
    stem = None
    renamed: dict[str, str] = {}
    for st in stages:
        json_path = work / f"netlist_{st}.json"
        log.info(f"Running synthesis ({st})...")
        synth.synthesize(args.top, json_path, stage=st)
        netlist = json.loads(json_path.read_text(encoding="utf-8"))
        root = find_top(netlist["modules"])
        if args.select:
            try:
                root, insts = resolve_instance(netlist["modules"], root, args.select)
            except VeriViewError as e:
                if st == args.stage:
                    raise
                log.warn(f"Stage '{st}' skipped: {e}")
                continue
            stem = re.sub(r"[^\w.-]+", "_", insts[-1])
        orig_root = root
        netlist, root = reroot(netlist, root)
        renamed[orig_root] = root
        stage_netlists[st] = netlist
        if args.keep_json:
            json_path.write_text(json.dumps(netlist), encoding="utf-8")

    out = _output_path(args, inputs, ".html", stem)
    fsms = _extract_fsms(args, synth, work)
    for old, new in renamed.items():  # --select renames a parameterized root module
        if old in fsms and old != new:
            fsms[new] = fsms.pop(old)
    build_viewer(stage_netlists, out, work, yosys=synth.yosys, skin=args.skin,
                 default_stage=args.stage, default_flat=args.flatten, fsms=fsms)
    if args.keep_json:
        for st in stage_netlists:
            shutil.copyfile(work / f"netlist_{st}.json", out.with_name(f"{out.stem}.{st}.json"))
    return out


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)
    log.set_quiet(args.quiet)

    inputs = _split_positionals(args, parser)
    inputs, args.stem_hint = _expand_dirs(inputs)

    # Output kind: the HTML viewer by default; a plain SVG with --svg, -o *.svg or SVG-only options
    out_ext = Path(args.output).suffix.lower() if args.output else ""
    report = bool(args.timing or args.fsm)
    if not report and args.svg and (args.html or out_ext == ".html"):
        raise VeriViewError("--svg and an HTML output cannot be combined.")
    if not report and args.html and out_ext == ".svg":
        raise VeriViewError("--html writes an .html file; use -o <name>.html (or drop --html for an SVG).")
    svg_only = args.split or args.cone or args.depth is not None
    want_svg = args.svg or out_ext == ".svg" or (svg_only and not args.html)
    explicit_html = args.html
    if not report and not want_svg:
        args.html = True
    if args.open is None:  # open the viewer when the user just typed `veriview design.sv`
        import sys
        args.open = bool(args.html and not args.output and not explicit_html and sys.stdout.isatty())
    if args.top is not None and not TOP_RE.match(args.top):
        raise VeriViewError(f"Invalid top module name: '{args.top}'")
    if args.skin and not args.skin.is_file():
        raise VeriViewError(f"Skin file not found: {args.skin}")
    if args.html and args.split:
        raise VeriViewError("--html and --split cannot be combined (the HTML viewer already contains every module).")
    if args.split and (args.select or args.depth is not None or args.cone):
        raise VeriViewError("--split cannot be combined with --select, --depth or --cone.")
    if args.flatten and args.depth is not None:
        raise VeriViewError("--flatten and --depth cannot be combined (--flatten expands every level).")
    if args.depth is not None and args.depth < 0:
        raise VeriViewError("--depth must be >= 0.")
    if args.fsm and (args.html or args.split or args.timing):
        raise VeriViewError("--fsm prints a report; it cannot be combined with --html, --split or --timing.")
    if args.timing and (args.html or args.split or args.cone or args.depth is not None):
        raise VeriViewError("--timing prints a report; it cannot be combined with --html, --split, --cone or --depth.")
    if args.html and (args.depth is not None or args.cone):
        raise VeriViewError("--depth and --cone are interactive in the HTML viewer "
                            "(navigate into sub-modules, click a net to highlight its cone).")
    html_stages = [s.strip() for s in args.html_stages.split(",") if s.strip()]
    bad = [s for s in html_stages if s not in STAGES]
    if bad or not html_stages:
        raise VeriViewError(f"Invalid --html-stages: {args.html_stages} (choose from {', '.join(STAGES)})")
    if args.stage not in html_stages:
        html_stages.insert(0, args.stage)

    ensure_dependencies(install=args.install_deps, assume_yes=args.yes,
                        want_sv2v=args.install_deps or args.frontend == "sv2v")
    if not inputs:
        if args.install_deps:
            log.info("Dependencies are installed.")
            return 0
        raise VeriViewError("No input file provided. Try 'veriview --help'.")
    warn_if_old_yosys()

    if args.watch:
        return watch(lambda: _build(args, inputs, html_stages), args=args, inputs=inputs)

    status, out, _ = _build(args, inputs, html_stages)
    if args.open and out is not None:
        open_path(out)
    return status


def _run_timing(args: argparse.Namespace, synth: Synthesizer, work: Path) -> int:
    from .timing_report import analyze, format_report
    json_path = work / "netlist_gate.json"
    log.info("Running synthesis (gate)...")
    synth.synthesize(args.top, json_path, stage="gate")
    netlist = json.loads(json_path.read_text(encoding="utf-8"))
    root = find_top(netlist["modules"])
    if args.select:
        root, _ = resolve_instance(netlist["modules"], root, args.select)
    netlist, root = reroot(netlist, root)
    netlist = postprocess(synth.yosys, work, netlist, root, flatten=True, cones=[])
    res = analyze(netlist["modules"][root], work, {
        "period": args.timing, "inputDelay": args.input_delay,
        "outputDelay": args.output_delay, "uncertainty": args.uncertainty,
    })
    print(format_report(res, root))
    return 0


def _extract_fsms(args: argparse.Namespace, synth: Synthesizer, work: Path, strict: bool = False) -> dict:
    from .fsm import SourceReader, extract_fsms, force_commands, recoding_rejects
    json_path = work / "netlist_fsm.json"
    log.info("Extracting state machines...")
    try:
        synth.synthesize(args.top, json_path, stage="fsm")
        # second pass for FSMs that fsm_detect skipped only because recoding would not pay off
        log_path = json_path.with_suffix(".detect.log")
        rejects = recoding_rejects(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.is_file() else []
        if rejects:
            synth.synthesize(args.top, json_path, stage="fsm", subst={"{force}": force_commands(rejects)})
        pre = json.loads(json_path.with_suffix(".pre.json").read_text(encoding="utf-8"))
        return extract_fsms(json.loads(json_path.read_text(encoding="utf-8")), SourceReader([Path.cwd(), work]), pre)
    except (VeriViewError, OSError, KeyError, ValueError) as e:
        if strict:
            raise
        log.warn(f"State machine extraction failed: {str(e).splitlines()[0]}")
        return {}


def _run_fsm(args: argparse.Namespace, synth: Synthesizer, work: Path) -> int:
    from .fsm import format_fsms, to_dot
    fsms = _extract_fsms(args, synth, work, strict=True)
    if args.output and args.output.endswith(".dot"):
        Path(args.output).write_text(to_dot(fsms), encoding="utf-8")
        log.info(f"Graphviz diagram saved: {args.output}")
    print(format_fsms(fsms))
    return 0


def _build(args: argparse.Namespace, inputs: list[str], html_stages: list[str]) -> BuildResult:
    """One complete run: returns (exit status, output path, sources)."""
    sources = collect_sources(inputs, args.incdirs, args.defines)

    with tempfile.TemporaryDirectory(prefix="veriview.") as tmp:
        work = Path(tmp).resolve()
        # --split flattens per module during synthesis; otherwise the hierarchical
        # netlist is narrowed/flattened afterwards (see scope.py).
        synth = Synthesizer(sources, work, frontend=args.frontend,
                            flatten=args.flatten and args.split, stage=args.stage)
        render_opts = dict(skin=args.skin, bg=args.bg, keep_json=args.keep_json)

        if args.split:
            out_dir = Path(args.output or ".")
            log.info("Listing modules...")
            modules = synth.list_modules()
            if not modules:
                raise VeriViewError("No modules found.")
            status = 0
            for m in modules:
                json_path = work / f"{m}.json"
                try:
                    synth.synthesize(m, json_path)
                    render(json_path, out_dir / f"{m}.svg", **render_opts)
                except VeriViewError as e:
                    log.error(f"Module '{m}': {e}")
                    status = 1
            return status, out_dir, sources

        if args.html:
            return 0, _run_html(args, inputs, synth, work, html_stages), sources

        if args.timing:
            return _run_timing(args, synth, work), None, sources

        if args.fsm:
            return _run_fsm(args, synth, work), None, sources

        json_path = work / "netlist.json"
        log.info("Running synthesis...")
        synth.synthesize(args.top, json_path)

        netlist = json.loads(json_path.read_text(encoding="utf-8"))
        root = find_top(netlist["modules"])
        stem = None
        if args.select:
            root, insts = resolve_instance(netlist["modules"], root, args.select)
            stem = re.sub(r"[^\w.-]+", "_", insts[-1])
            log.info(f"Selected instance: {'/'.join(insts)}")
        netlist, root = reroot(netlist, root, keep_below=args.depth)
        netlist = postprocess(synth.yosys, work, netlist, root,
                              flatten=args.flatten or args.depth is not None, cones=args.cone)
        json_path.write_text(json.dumps(netlist), encoding="utf-8")

        svg = _output_path(args, inputs, ".svg", stem)
        log.info(f"Generating schematic -> {svg}")
        render(json_path, svg, **render_opts)
    return 0, svg, sources


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except VeriViewError as e:
        log.error(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
