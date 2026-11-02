"""End-to-end tests: run VeriView on the example designs (needs yosys + netlistsvg)."""

import json
import shutil
from pathlib import Path

import pytest
from conftest import EX, needs_sv, needs_tools

from veriview.cli import main

pytestmark = needs_tools


def vv(*args) -> int:
    return main(["-q", *map(str, args)])


def is_svg(p: Path) -> bool:
    return p.is_file() and p.stat().st_size > 0 and "<svg" in p.read_text()


def cells(json_path: Path, module: str):
    return [c["type"] for c in json.loads(json_path.read_text())["modules"][module]["cells"].values()]


@pytest.mark.parametrize("name, args", [
    ("counter", [EX / "counter.sv"]),
    ("seqdect", [EX / "seqdect.sv"]),
    ("mux_basic", [EX / "mux" / "mux_basic.sv"]),
    ("rca", [EX / "rca" / "rca.f"]),
    ("fa_legacy_top", [EX / "rca" / "rca.f", "fa"]),
    ("fa_top", [EX / "rca" / "rca.f", "-t", "fa"]),
    ("rca_flat", [EX / "rca" / "rca.f", "--flatten"]),
    ("gray_flat", [EX / "gray" / "gray.f", "--flatten"]),
    ("gray_f_flag", ["-f", EX / "gray" / "gray.f", "-t", "b2g2b"]),
    ("multi", [EX / "rca" / "ha.sv", EX / "rca" / "fa.sv"]),
])
def test_examples(tmp_path, name, args):
    out = tmp_path / f"{name}.svg"
    assert vv(*args, "-o", out) == 0
    assert is_svg(out)


def test_rca_structure(tmp_path):
    """The hierarchical RCA must instantiate 8 full adders."""
    assert vv(EX / "rca" / "rca.f", "-o", tmp_path / "rca.svg", "--keep-json") == 0
    assert cells(tmp_path / "rca.json", "rca").count("fa") == 8


def test_flatten_removes_submodule_instances(tmp_path):
    assert vv(EX / "gray" / "gray.f", "-o", tmp_path / "h.svg", "--keep-json") == 0
    hier = cells(tmp_path / "h.json", "b2g2b")
    assert len(hier) == 2 and any("b2g" in c for c in hier) and any("g2b" in c for c in hier)
    assert vv(EX / "gray" / "gray.f", "--flatten", "-o", tmp_path / "f.svg", "--keep-json") == 0
    flat = cells(tmp_path / "f.json", "b2g2b")
    assert flat and all(c.startswith("$") and "paramod" not in c for c in flat)


@needs_sv
def test_unpacked_array_ports(tmp_path):
    assert vv(EX / "mux" / "mux.sv", "-o", tmp_path / "mux.svg") == 0
    assert is_svg(tmp_path / "mux.svg")


@needs_sv
def test_shift_reg(tmp_path):
    assert vv(EX / "shift_reg.sv", "-o", tmp_path / "sr.svg") == 0
    assert is_svg(tmp_path / "sr.svg")


@needs_sv
def test_shift_reg_param_check(tmp_path, capsys):
    src = (EX / "shift_reg.sv").read_text().replace("NUM_REGS   = 2", "NUM_REGS   = 0")
    (tmp_path / "sr0.sv").write_text(src)
    assert vv(tmp_path / "sr0.sv", "-o", tmp_path / "x.svg") == 1
    assert "NUM_REGS must be" in capsys.readouterr().err
    assert not (tmp_path / "x.svg").exists()


def test_split(tmp_path):
    assert vv(EX / "rca" / "rca.f", "--split", "-o", tmp_path / "split") == 0
    for m in ("rca", "fa", "ha"):
        assert is_svg(tmp_path / "split" / f"{m}.svg")


def test_background_options(tmp_path):
    assert vv(EX / "counter.sv", "-o", tmp_path / "bg.svg", "--bg", "#123456") == 0
    assert 'fill="#123456"' in (tmp_path / "bg.svg").read_text()
    assert vv(EX / "counter.sv", "-o", tmp_path / "nobg.svg", "--bg", "none") == 0
    assert 'width="100%" height="100%"' not in (tmp_path / "nobg.svg").read_text()


def test_output_directory(tmp_path):
    assert vv(EX / "counter.sv", "-o", f"{tmp_path}/dir/") == 0
    assert is_svg(tmp_path / "dir" / "counter.svg")


def test_default_output_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert vv(EX / "rca" / "rca.f", "-t", "fa") == 0
    assert is_svg(tmp_path / "fa.svg")


def test_path_with_spaces(tmp_path):
    d = tmp_path / "with space"
    d.mkdir()
    shutil.copy(EX / "counter.sv", d)
    assert vv(d / "counter.sv", "-o", d / "c.svg") == 0
    assert is_svg(d / "c.svg")


def test_filelist_defines_includes(tmp_path, monkeypatch):
    inc = tmp_path / "inc dir"
    inc.mkdir()
    (inc / "defs.svh").write_text("`define W 3\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "top.sv").write_text(
        '`include "defs.svh"\n'
        "module top(input logic [`W-1:0] a, output logic [`W-1:0] y);\n"
        "`ifdef INVERT\n  assign y = ~a;\n`else\n  assign y = a;\n`endif\n"
        "endmodule\n"
    )
    (tmp_path / "sub" / "inner.f").write_text("$(SUBDIR)/top.sv  // comment\n")
    (tmp_path / "main.f").write_text("+define+INVERT\n-f ${NEST}/inner.f\n")
    monkeypatch.setenv("SUBDIR", ".")
    monkeypatch.setenv("NEST", "sub")

    assert vv(tmp_path / "main.f", "-I", inc, "-o", tmp_path / "out.svg", "--keep-json") == 0
    assert "$not" in cells(tmp_path / "out.json", "top")

    assert vv(tmp_path / "sub" / "top.sv", "-I", inc, "-o", tmp_path / "plain.svg", "--keep-json") == 0
    assert "$not" not in cells(tmp_path / "plain.json", "top")


@pytest.mark.parametrize("args, msg", [
    ([], "No input file"),
    ([EX / "nope.sv"], "not found"),
    ([EX / "counter.sv", "-t", "nosuch"], "not found"),
])
def test_errors(tmp_path, capsys, args, msg):
    assert vv(*args, "-o", tmp_path / "x.svg") == 1
    assert msg in capsys.readouterr().err
    assert not (tmp_path / "x.svg").exists()


def test_syntax_error(tmp_path, capsys):
    (tmp_path / "broken.v").write_text("module broken(input a, output y);\n  assign y = ;\nendmodule\n")
    assert vv(tmp_path / "broken.v", "-o", tmp_path / "x.svg") == 1
    assert "syntax error" in capsys.readouterr().err
