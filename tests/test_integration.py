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


def _viewer_data(html: Path) -> dict:
    import re
    text = html.read_text()
    # two scripts: the simulation engine (sim.js) and the viewer; SVG/JSON must not add any
    assert text.count("</script>") == 2
    return json.loads(re.search(r"const DATA = (.*?);\n", text).group(1))


def test_html_viewer(tmp_path):
    out = tmp_path / "rca.html"
    assert vv(EX / "rca" / "rca.f", "--html", "-o", out) == 0
    data = _viewer_data(out)
    assert data["stages"] == ["elab", "opt", "gate"] and data["defaultStage"] == "opt"
    for st in data["stages"]:
        view = data["views"][st]
        assert view["top"] == "rca"
        mods = view["modules"]
        assert set(mods) == {"rca", "fa", "ha"}
        assert len(mods["rca"]["children"]) == 8
        assert set(mods["fa"]["children"].values()) == {"ha"}
        # flattened variants only for modules that contain sub-modules
        assert "flat" in mods["rca"] and "flat" in mods["fa"] and "flat" not in mods["ha"]
        assert mods["rca"]["flat"]["cells"] == 40
        # every navigable instance must exist as a clickable cell in the SVG
        for inst in mods["rca"]["children"]:
            assert f'id="cell_{inst}"' in mods["rca"]["hier"]["svg"]
    gate_types = {c[0] for c in data["views"]["gate"]["modules"]["ha"]["hier"]["net"]["cells"].values()}
    assert gate_types and all(t.startswith("$_") for t in gate_types)


def test_html_viewer_netlist_for_cones(tmp_path):
    out = tmp_path / "seq.html"
    assert vv(EX / "seqdect.sv", "--html", "--html-stages", "opt", "-o", out) == 0
    data = _viewer_data(out)
    assert data["stages"] == ["opt"]
    net = data["views"]["opt"]["modules"]["seqdect"]["hier"]["net"]
    assert net["ports"]["o_detected"][0] == "output"
    assert "shift_reg" in net["names"]
    types = {c[0] for c in net["cells"].values()}
    assert "$adff" in types and "$eq" in types
    # every cell connection carries a direction (needed to walk cones)
    for cell in net["cells"].values():
        conns = cell[1]
        assert all(d in ("input", "output", "inout") for d, _ in conns.values())


def test_html_viewer_parameter_labels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert vv(EX / "gray" / "gray.f", "--html", "--html-stages", "opt") == 0
    labels = {m["label"] for m in _viewer_data(tmp_path / "gray.html")["views"]["opt"]["modules"].values()}
    assert labels == {"b2g2b", "b2g (WIDTH=3)", "g2b (WIDTH=3)"}


def test_html_select_and_stage(tmp_path):
    out = tmp_path / "s.html"
    assert vv(EX / "rca" / "rca.f", "--html", "--select", "fa_block[1].fa_inst", "--stage", "gate",
              "--html-stages", "opt", "-o", out) == 0
    data = _viewer_data(out)
    assert data["stages"] == ["gate", "opt"] and data["defaultStage"] == "gate"
    assert data["views"]["gate"]["top"] == "fa"


@pytest.mark.parametrize("args, msg", [
    (["--split"], "cannot be combined"),
    (["--cone", "count"], "interactive in the HTML viewer"),
    (["--depth", "1"], "interactive in the HTML viewer"),
    (["--html-stages", "foo"], "Invalid --html-stages"),
])
def test_html_option_errors(tmp_path, capsys, args, msg):
    assert vv(EX / "counter.sv", "--html", *args, "-o", tmp_path / "x.html") == 1
    assert msg in capsys.readouterr().err


DEAD_LOGIC = """
module leaf(input [3:0] a, output [3:0] y); assign y = a + 4'd1; endmodule
module top(input [3:0] a, output [3:0] y);
  wire [3:0] unused;
  leaf used_inst (.a(a), .y(y));
  leaf dead_inst (.a(a), .y(unused));
  wire [3:0] dead_logic = a ^ 4'hF;
endmodule
"""


@pytest.mark.parametrize("stage, expected", [
    ("elab", ["$not", "leaf", "leaf"]),   # RTL as written: nothing removed
    ("opt", ["leaf"]),                    # unused instance and logic optimized away
    ("gate", ["leaf"]),
])
def test_stages(tmp_path, stage, expected):
    (tmp_path / "dead.v").write_text(DEAD_LOGIC)
    assert vv(tmp_path / "dead.v", "--stage", stage, "-o", tmp_path / "d.svg", "--keep-json") == 0
    assert sorted(cells(tmp_path / "d.json", "top")) == expected


def test_stage_gate_uses_gates(tmp_path):
    assert vv(EX / "counter.sv", "--stage", "gate", "-o", tmp_path / "g.svg", "--keep-json") == 0
    types = set(cells(tmp_path / "g.json", "counter"))
    assert types and all(t.startswith("$_") for t in types)


def test_select_instance(tmp_path):
    assert vv(EX / "rca" / "rca.f", "--select", "fa_block[3].fa_inst", "-o", tmp_path / "s.svg", "--keep-json") == 0
    assert sorted(cells(tmp_path / "s.json", "fa")) == ["$or", "ha", "ha"]
    assert vv(EX / "rca" / "rca.f", "--select", "fa_block[3].fa_inst/ha2", "-o", tmp_path / "h.svg") == 0


def test_select_parameterized(tmp_path):
    assert vv(EX / "gray" / "gray.f", "--select", "u_b2g", "-o", tmp_path / "b.svg", "--keep-json") == 0
    assert cells(tmp_path / "b.json", "b2g") == ["$xor"]


def test_select_default_output_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert vv(EX / "rca" / "rca.f", "--select", "fa_block[3].fa_inst") == 0
    assert is_svg(tmp_path / "fa_block_3_.fa_inst.svg")


def test_depth(tmp_path):
    assert vv(EX / "rca" / "rca.f", "--depth", "0", "-o", tmp_path / "d0.svg", "--keep-json") == 0
    assert sorted(set(cells(tmp_path / "d0.json", "rca"))) == ["fa"]
    assert vv(EX / "rca" / "rca.f", "--depth", "1", "-o", tmp_path / "d1.svg", "--keep-json") == 0
    c = cells(tmp_path / "d1.json", "rca")
    assert c.count("ha") == 16 and c.count("$or") == 8


def test_cone(tmp_path):
    assert vv(EX / "seqdect.sv", "--cone", "o_detected", "-o", tmp_path / "c.svg", "--keep-json") == 0
    assert sorted(cells(tmp_path / "c.json", "seqdect")) == ["$adff", "$eq"]
    # carry chain only: the 8 sum XORs are not in the cone of cout
    assert vv(EX / "rca" / "rca.f", "--flatten", "--cone", "cout", "-o", tmp_path / "k.svg", "--keep-json") == 0
    assert cells(tmp_path / "k.json", "rca").count("$xor") == 8


@pytest.mark.parametrize("args, msg", [
    (["--select", "nope"], "No sub-module instance"),
    (["--cone", "nosig"], "not found"),
    (["--flatten", "--depth", "1"], "cannot be combined"),
    (["--split", "--cone", "cout"], "cannot be combined"),
])
def test_scope_errors(tmp_path, capsys, args, msg):
    assert vv(EX / "rca" / "rca.f", *args, "-o", tmp_path / "x.svg") == 1
    assert msg in capsys.readouterr().err


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
