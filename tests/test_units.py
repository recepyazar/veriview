"""Unit tests for logic that doesn't need external tools."""

from pathlib import Path

import pytest

from veriview.cli import build_parser, run
from veriview.filelist import SourceSet, collect_sources, expand_env, parse_filelist
from veriview.log import VeriViewError
from veriview.render import add_background
from veriview.synth import fix_sv2v_output, yosys_quote

# --- expand_env ---------------------------------------------------------------

def test_expand_env_forms(monkeypatch):
    monkeypatch.setenv("A", "x")
    monkeypatch.delenv("NOPE", raising=False)
    assert expand_env("$A/${A}/$(A)/$NOPE/end") == "x/x/x//end"


# --- file lists ---------------------------------------------------------------

def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_filelist_full_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("SUB", "sub")
    _write(tmp_path / "sub" / "inner.f", "deep.sv // trailing comment\n")
    f = _write(tmp_path / "main.f", "\n".join([
        "# hash comment",
        "// slash comment",
        "+incdir+inc+other",
        "+define+A+B=2",
        "-f ${SUB}/inner.f",
        "-v lib/cells.v",
        "-Iextra -D C=3",
        "dir with space/top.sv",
        "/abs/x.sv",
        "-y libdir +libext+.v",
    ]))
    out = parse_filelist(f, SourceSet())
    assert out.files == [
        tmp_path / "sub" / "deep.sv",
        tmp_path / "lib" / "cells.v",
        tmp_path / "dir with space" / "top.sv",
        Path("/abs/x.sv"),
    ]
    assert out.incdirs == [tmp_path / "inc", tmp_path / "other", tmp_path / "extra"]
    assert out.defines == ["A", "B=2", "C=3"]


def test_filelist_recursion(tmp_path):
    f = _write(tmp_path / "self.f", "-f self.f\n")
    with pytest.raises(VeriViewError, match="Recursive"):
        parse_filelist(f, SourceSet())


def test_filelist_missing(tmp_path):
    with pytest.raises(VeriViewError, match="File list not found"):
        parse_filelist(tmp_path / "nope.f", SourceSet())


def test_collect_sources_validation(tmp_path):
    with pytest.raises(VeriViewError, match="Source file not found"):
        collect_sources([str(tmp_path / "nope.sv")], [], [])
    src = _write(tmp_path / "a.sv", "module a; endmodule\n")
    with pytest.raises(VeriViewError, match="Invalid define"):
        collect_sources([str(src)], [], ["A B"])
    s = collect_sources([str(src)], [], ["OK=1"])
    assert s.has_systemverilog


# --- helpers ------------------------------------------------------------------

def test_add_background():
    svg = '<?xml version="1.0"?>\n<svg width="10" height="5"><g/></svg>'
    out = add_background(svg, "#123456")
    assert '<svg width="10" height="5"><rect x="0" y="0" width="100%" height="100%" fill="#123456"/><g/>' in out
    assert out.count("<rect") == 1
    assert add_background(svg, "none") == svg


def test_fix_sv2v_output():
    src = ('\t\t\tinitial $display("Error [elaboration] a.sv:17:5 - m.chk\\n msg: ", "N must be >= 1");\n'
           '\t\t\tinitial $display("Warning [elaboration] a.sv:3:1 - m\\n msg: ", "careful");\n'
           '\tinitial $display("hello");\n')
    out = fix_sv2v_output(src)
    assert '$error("N must be >= 1");' in out
    assert '$warning("careful");' in out
    assert 'initial $display("hello");' in out


def test_yosys_quote():
    assert yosys_quote('a b/"c"\\d') == '"a b/\\"c\\"\\\\d"'


# --- CLI parsing --------------------------------------------------------------

def test_parser_intermixed():
    args = build_parser().parse_intermixed_args(["a.f", "-o", "x.svg", "top", "--flatten", "-Iinc", "-DX=1"])
    assert args.inputs == ["a.f", "top"]
    assert args.output == "x.svg" and args.flatten
    assert args.incdirs == ["inc"] and args.defines == ["X=1"]


@pytest.mark.parametrize("argv, msg", [
    (["x.sv", "-t", "a;b"], "Invalid top module name"),
    (["x.sv", "a", "b"], None),  # argparse error -> SystemExit
])
def test_cli_rejects(argv, msg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "x.sv", "module x; endmodule\n")
    if msg:
        with pytest.raises(VeriViewError, match=msg):
            run(argv)
    else:
        with pytest.raises(SystemExit):
            run(argv)


@pytest.mark.parametrize("argv", [["--bogus"], ["x.sv", "-o"], ["x.sv", "--frontend", "foo"]])
def test_cli_argparse_errors(argv):
    with pytest.raises(SystemExit) as e:
        run(argv)
    assert e.value.code == 2
