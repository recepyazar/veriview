"""Command-line interface."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from . import __version__, log
from .filelist import collect_sources
from .log import VeriViewError
from .render import open_path, render
from .synth import FRONTENDS, TOP_RE, Synthesizer
from .tools import ensure_dependencies

HDL_SUFFIXES = (".v", ".sv", ".svh", ".vh", ".f")

EPILOG = """\
file list (.f) syntax:
  one source per line, // and # comments, +incdir+<dir>[+<dir>...],
  +define+<N>[=<V>][+...], -f/-F <nested.f>, -v <lib file>, -I<dir>, -D<N>,
  $VAR / ${VAR} / $(VAR) environment variables.
  Relative paths are resolved against the directory of the .f file.

examples:
  veriview counter.sv
  veriview design.f -t cpu_core -o build/cpu.svg
  veriview rtl/*.sv -I rtl/include -D SYNTH --flatten --open
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veriview",
        description="Generate SVG schematics from Verilog/SystemVerilog using Yosys and netlistsvg.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="*", metavar="input",
                   help=".v/.sv/.svh files and/or .f file lists (a trailing non-file word is the top module)")
    p.add_argument("-t", "--top", help="top module (default: auto-detected)")
    p.add_argument("-o", "--output", help="output SVG path (with --split: output directory)")
    p.add_argument("-f", "-F", dest="filelists", action="append", default=[], metavar="FILE.f",
                   help="read a file list (repeatable)")
    p.add_argument("-I", "--incdir", dest="incdirs", action="append", default=[], metavar="DIR",
                   help="include directory (repeatable)")
    p.add_argument("-D", "--define", dest="defines", action="append", default=[], metavar="N[=V]",
                   help="preprocessor define (repeatable)")
    p.add_argument("--flatten", action="store_true", help="flatten hierarchy (expand sub-modules)")
    p.add_argument("--split", action="store_true", help="render every module to its own SVG")
    p.add_argument("--frontend", choices=FRONTENDS, default="auto",
                   help="SystemVerilog frontend (default: auto = yosys, falling back to slang/sv2v)")
    p.add_argument("--skin", type=Path, help="custom netlistsvg skin")
    p.add_argument("--bg", default="white", metavar="COLOR|none", help="background color (default: white)")
    p.add_argument("--keep-json", action="store_true", help="keep the Yosys JSON netlist next to the SVG")
    p.add_argument("--open", action="store_true", help="open the result")
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


def _output_path(args: argparse.Namespace, inputs: list[str]) -> Path:
    stem = args.top or Path(inputs[0]).stem
    if args.output:
        out = Path(args.output)
        if args.output.endswith(("/", "\\")) or out.is_dir():
            return out / f"{stem}.svg"
        return out
    return Path(f"{stem}.svg")


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)
    log.set_quiet(args.quiet)

    inputs = _split_positionals(args, parser)
    if args.top is not None and not TOP_RE.match(args.top):
        raise VeriViewError(f"Invalid top module name: '{args.top}'")
    if args.skin and not args.skin.is_file():
        raise VeriViewError(f"Skin file not found: {args.skin}")

    ensure_dependencies(install=args.install_deps, assume_yes=args.yes,
                        want_sv2v=args.install_deps or args.frontend == "sv2v")
    if not inputs:
        if args.install_deps:
            log.info("Dependencies are installed.")
            return 0
        raise VeriViewError("No input file provided. Try 'veriview --help'.")

    sources = collect_sources(inputs, args.incdirs, args.defines)

    with tempfile.TemporaryDirectory(prefix="veriview.") as tmp:
        work = Path(tmp).resolve()
        synth = Synthesizer(sources, work, frontend=args.frontend, flatten=args.flatten)
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
            if args.open:
                open_path(out_dir)
            return status

        svg = _output_path(args, inputs)
        json_path = work / "netlist.json"
        log.info("Running synthesis...")
        synth.synthesize(args.top, json_path)
        log.info(f"Generating schematic -> {svg}")
        render(json_path, svg, **render_opts)
        if args.open:
            open_path(svg)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except VeriViewError as e:
        log.error(str(e))
        return 1
    except KeyboardInterrupt:
        return 130
