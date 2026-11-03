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


# --- HTML viewer helpers ------------------------------------------------------

def test_decode_param():
    from veriview.html import decode_param
    assert decode_param("s32'00000000000000000000000000000011") == "3"
    assert decode_param("s4'1111") == "-1"
    assert decode_param("4'1111") == "15"
    assert decode_param("8'1x00zz11") == "8'1x00zz11"


def test_display_name():
    from veriview.html import display_name
    assert display_name("fa") == "fa"
    assert display_name("$paramod\\b2g\\WIDTH=s32'00000000000000000000000000000011") == "b2g (WIDTH=3)"
    two = "$paramod\\mux\\A=s32'" + "0" * 29 + "100\\B=s32'" + "0" * 30 + "10"
    assert display_name(two) == "mux (A=4, B=2)"
    assert display_name("$paramod$0123abcd\\very_long", {"hdlname": "very_long"}) == "very_long"


def test_find_top():
    from veriview.scope import find_top
    assert find_top({"a": {}, "b": {"attributes": {"top": "00000000000000000000000000000001"}}}) == "b"
    assert find_top({"a": {}, "b": {"attributes": {"top": 0}}}) == "a"


# --- scope (--select / --depth) and stages ------------------------------------

def _netlist():
    def mod(cells, top=False):
        attrs = {"top": "00000000000000000000000000000001"} if top else {}
        return {"attributes": attrs, "ports": {}, "cells": {k: {"type": v} for k, v in cells.items()}}
    p = "$paramod\\mid\\W=s32'00000000000000000000000000000100"
    return {"modules": {
        "top": mod({"u_mid": p, "gen[0].u_leaf": "leaf", "x": "$and"}, top=True),
        p: mod({"l0": "leaf"}) | {"attributes": {"hdlname": "mid"}},
        "leaf": mod({"n": "$not"}),
        "unused": mod({}),
    }}


def test_resolve_instance():
    from veriview.scope import resolve_instance
    mods = _netlist()["modules"]
    assert resolve_instance(mods, "top", "u_mid")[0].startswith("$paramod\\mid")
    assert resolve_instance(mods, "top", "u_mid/l0") == ("leaf", ["u_mid", "l0"])
    assert resolve_instance(mods, "top", "u_mid.l0") == ("leaf", ["u_mid", "l0"])
    assert resolve_instance(mods, "top", "gen[0].u_leaf") == ("leaf", ["gen[0].u_leaf"])
    with pytest.raises(VeriViewError, match="No sub-module instance 'nope'"):
        resolve_instance(mods, "top", "nope")


def test_reroot_and_depth():
    from veriview.scope import find_top, module_depths, reroot
    net = _netlist()
    mods = net["modules"]
    p = "$paramod\\mid\\W=s32'00000000000000000000000000000100"
    assert module_depths(mods, "top") == {"top": 0, p: 1, "leaf": 1}

    out, root = reroot(net, p)
    assert root == "mid" and set(out["modules"]) == {"mid", "leaf"}
    assert find_top(out["modules"]) == "mid"
    assert "top" not in out["modules"]["leaf"]["attributes"]

    out, root = reroot(net, "top", keep_below=0)
    assert root == "top" and "unused" not in out["modules"]
    assert "keep_hierarchy" in out["modules"]["leaf"]["attributes"]
    assert "keep_hierarchy" not in out["modules"]["top"]["attributes"]


def test_stage_commands():
    from veriview.synth import stage_commands
    assert stage_commands("opt", False) == ["proc", "opt", "opt_clean -purge"]
    assert "flatten" in stage_commands("elab", True)
    assert any(c.startswith("setattr -set keep") for c in stage_commands("elab", False))
    assert any(c.startswith("abc -g") for c in stage_commands("gate", False))


def test_old_yosys_compat():
    from veriview.html import display_name
    from veriview.synth import yosys_path_arg
    # Yosys 0.33 does not unquote option values such as `tee -o`
    assert yosys_path_arg("/tmp/vv/modules.txt") == "/tmp/vv/modules.txt"
    assert yosys_path_arg("/tmp/a b/modules.txt") == '"/tmp/a b/modules.txt"'
    # ... and stores hdlname with a leading backslash
    name = "$paramod\\b2g\\WIDTH=s32'00000000000000000000000000000011"
    assert display_name(name, {"hdlname": "\\b2g"}) == "b2g (WIDTH=3)"
