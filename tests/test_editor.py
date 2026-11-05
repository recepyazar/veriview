"""Tests for the gate editor's Verilog generation (src/veriview/editor_core.js).

A design drawn with the editor is turned into Verilog by node, compiled by the real
pipeline and simulated with sim.js: the truth table must match the drawing.
"""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import needs_tools
from test_sim import build, simulate

CORE = Path(__file__).resolve().parent.parent / "src" / "veriview" / "editor_core.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node")


def to_verilog(tmp_path: Path, design: dict, name: str = "drawn") -> dict:
    script = tmp_path / "gen.js"
    script.write_text(f"const E = require({json.dumps(str(CORE))});\n"
                      f"console.log(JSON.stringify(E.toVerilog({json.dumps(design)}, {json.dumps(name)})));")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def W(a, ap, b, bp):
    return {"from": {"node": a, "pin": ap}, "to": {"node": b, "pin": bp}}


def node(i, t, **kw):
    return {"id": i, "type": t, "x": 0, "y": 0, **kw}


# full adder + 3-input NAND + mux + a resettable register of the sum
DESIGN = {
    "nodes": [
        node(1, "in", name="a"), node(2, "in", name="b"), node(3, "in", name="cin"), node(4, "in", name="sel"),
        node(5, "in", name="clk"), node(6, "in", name="rst_n"),
        node(10, "xor"), node(11, "xor"), node(12, "and"), node(13, "and"), node(14, "or"),
        node(15, "nand", n=3), node(16, "mux"), node(17, "dffr"),
        node(20, "out", name="sum"), node(21, "out", name="cout"), node(22, "out", name="nand3"),
        node(23, "out", name="m"), node(24, "out", name="q"),
    ],
    "wires": [
        W(1, "Y", 10, "A"), W(2, "Y", 10, "B"), W(10, "Y", 11, "A"), W(3, "Y", 11, "B"),
        W(1, "Y", 12, "A"), W(2, "Y", 12, "B"), W(10, "Y", 13, "A"), W(3, "Y", 13, "B"),
        W(12, "Y", 14, "A"), W(13, "Y", 14, "B"),
        W(1, "Y", 15, "A"), W(2, "Y", 15, "B"), W(3, "Y", 15, "C"),
        W(1, "Y", 16, "A"), W(2, "Y", 16, "B"), W(4, "Y", 16, "S"),
        W(11, "Y", 17, "D"), W(5, "Y", 17, "CLK"), W(6, "Y", 17, "RST_N"),
        W(11, "Y", 20, "A"), W(14, "Y", 21, "A"), W(15, "Y", 22, "A"), W(16, "Y", 23, "A"), W(17, "Q", 24, "A"),
    ],
}


def test_generated_verilog_text(tmp_path):
    res = to_verilog(tmp_path, DESIGN)
    text = res["text"]
    assert res["warn"] == []
    assert "module drawn (" in text and "endmodule" in text
    assert "assign xor10 = a ^ b;" in text
    assert "assign nand15 = ~(a & b & cin);" in text
    assert "assign mux16 = sel ? b : a;" in text
    assert "always @(posedge clk or negedge rst_n)" in text and "if (!rst_n) q17 <= 1'b0;" in text


def test_warnings(tmp_path):
    d = {"nodes": [node(1, "in", name="a"), node(2, "and"), node(3, "not"), node(4, "not"), node(5, "out", name="y"),
                   node(6, "out", name="y")],
         "wires": [W(1, "Y", 2, "A"), W(3, "Y", 4, "A"), W(4, "Y", 3, "A")]}
    warn = " | ".join(to_verilog(tmp_path, d)["warn"])
    assert "unconnected input B of AND2" in warn
    assert "combinational loop through" in warn
    assert "output y is not driven" in warn
    assert "duplicate port name y" in warn


@needs_tools
@pytest.mark.parametrize("stage", ["opt", "gate"])
def test_drawn_design_simulates_correctly(tmp_path, stage):
    src = tmp_path / "drawn.v"
    src.write_text(to_verilog(tmp_path, DESIGN)["text"])
    data = build(tmp_path, src, stage=stage)
    ops, exp = [["set", "rst_n", 1]], []
    for a, b, cin, sel in itertools.product((0, 1), repeat=4):
        ops += [["set", "a", a], ["set", "b", b], ["set", "cin", cin], ["set", "sel", sel],
                ["get", "sum"], ["get", "cout"], ["get", "nand3"], ["get", "m"]]
        exp += [a ^ b ^ cin, (a & b) | ((a ^ b) & cin), 1 - (a & b & cin), b if sel else a]
    # the register captures the sum on the clock edge; the asynchronous reset clears it
    ops += [["set", "a", 1], ["set", "b", 0], ["set", "cin", 0], ["clock", "clk", 1], ["get", "q"],
            ["set", "rst_n", 0], ["get", "q"]]
    exp += [1, 0]
    assert simulate(tmp_path, data, stage, ops) == exp


def test_net_names(tmp_path):
    d = {"nodes": [node(1, "in", name="a"), node(2, "in", name="b"), node(3, "and", label="carry"),
                   node(4, "xor", label="a"), node(5, "or", label="wire"), node(6, "not", label="9x"),
                   node(7, "dff", label="state"), node(8, "out", name="c")],
         "wires": [W(1, "Y", 3, "A"), W(2, "Y", 3, "B"), W(3, "Y", 8, "A"), W(3, "Y", 7, "D")]}
    res = to_verilog(tmp_path, d)
    text, warn = res["text"], " | ".join(res["warn"])
    assert "assign carry = a & b;" in text and "assign c = carry;" in text
    assert "reg  state;" in text and "state <= carry;" in text
    # clashes with a port, keywords and invalid identifiers fall back to the automatic name
    assert "name a is used twice" in warn and "assign xor4 =" in text
    assert "invalid name wire" in warn and "assign or5 =" in text
    assert "invalid name 9x" in warn and "assign not6 =" in text


@needs_tools
def test_named_design_compiles(tmp_path):
    d = {"nodes": [node(1, "in", name="a"), node(2, "in", name="b"), node(3, "xor", label="sum_bit"),
                   node(4, "and", label="carry"), node(5, "out", name="s"), node(6, "out", name="c")],
         "wires": [W(1, "Y", 3, "A"), W(2, "Y", 3, "B"), W(1, "Y", 4, "A"), W(2, "Y", 4, "B"),
                   W(3, "Y", 5, "A"), W(4, "Y", 6, "A")]}
    src = tmp_path / "ha.v"
    src.write_text(to_verilog(tmp_path, d, "ha")["text"])
    data = build(tmp_path, src, stage="elab")
    ops = []
    for a, b in itertools.product((0, 1), repeat=2):
        ops += [["set", "a", a], ["set", "b", b], ["get", "s"], ["get", "c"]]
    assert simulate(tmp_path, data, "elab", ops) == [0, 0, 1, 0, 1, 0, 0, 1]
    # the names survive into the viewer's RTL view (the optimized view may merge them into the ports)
    names = json.loads(data.read_text())["views"]["elab"]["modules"]["ha"]["hier"]["net"]["names"]
    assert {"sum_bit", "carry"} <= set(names)


# net labels (Logisim-style tunnels): a wire into tag "c" drives every tag named "c"
TAGGED = {
    "nodes": [node(1, "in", name="a"), node(2, "in", name="b"), node(3, "and"), node(4, "tag", name="carry"),
              node(5, "tag", name="carry"), node(6, "not"), node(7, "out", name="y"), node(8, "tag", name="carry"),
              node(9, "out", name="c")],
    "wires": [W(1, "Y", 3, "A"), W(2, "Y", 3, "B"), W(3, "Y", 4, "T"), W(5, "T", 6, "A"), W(6, "Y", 7, "A"),
              W(8, "T", 9, "A")],
}


def test_net_labels_connect(tmp_path):
    res = to_verilog(tmp_path, TAGGED)
    text = res["text"]
    assert res["warn"] == []
    # the label names the driving gate's net, and every tag with that name reads it
    assert "assign carry = a & b;" in text
    assert "assign not6 = ~carry;" in text and "assign c = carry;" in text


def test_net_label_warnings(tmp_path):
    d = {"nodes": [node(1, "in", name="a"), node(2, "tag", name="x"), node(3, "out", name="y"),
                   node(4, "in", name="b"), node(5, "tag", name="z"), node(6, "tag", name="z"),
                   node(7, "tag", name="z"), node(8, "out", name="w")],
         "wires": [W(2, "T", 3, "A"), W(1, "Y", 5, "T"), W(4, "Y", 6, "T"), W(7, "T", 8, "A")]}
    warn = " | ".join(to_verilog(tmp_path, d)["warn"])
    assert "label x has no driver" in warn
    assert "label z has several drivers" in warn


@needs_tools
def test_tagged_design_simulates(tmp_path):
    src = tmp_path / "tagged.v"
    src.write_text(to_verilog(tmp_path, TAGGED, "tagged")["text"])
    data = build(tmp_path, src, stage="opt")
    ops, exp = [], []
    for a, b in itertools.product((0, 1), repeat=2):
        ops += [["set", "a", a], ["set", "b", b], ["get", "y"], ["get", "c"]]
        exp += [1 - (a & b), a & b]
    assert simulate(tmp_path, data, "opt", ops) == exp


@needs_tools
def test_module_opened_in_editor_behaves_the_same(tmp_path):
    """counter.sv -> editor drawing (to_editor.py) -> Verilog (editor_core.js) -> simulation: still counts."""
    from conftest import EX

    from veriview.filelist import collect_sources
    from veriview.synth import Synthesizer
    from veriview.to_editor import netlist_to_design
    out = tmp_path / "e.json"
    Synthesizer(collect_sources([str(EX / "counter.sv")], [], []), tmp_path).synthesize("counter", out, stage="editor")
    d = netlist_to_design(json.loads(out.read_text()))
    assert {n["name"] for n in d["nodes"] if n["type"] == "out"} == {"count_0", "count_1", "count_2", "count_3"}
    assert len({(n["x"], n["y"]) for n in d["nodes"]}) == len(d["nodes"])       # no two nodes on top of each other
    res = to_verilog(tmp_path, d, d["module"])
    assert res["warn"] == []
    src = tmp_path / "counter_gates.v"
    src.write_text(res["text"])
    data = build(tmp_path, src, stage="opt")
    ops = [["set", "rst_n", 1]]
    exp = []
    for i in range(1, 19):
        ops += [["clock", "clk", 1]] + [["get", f"count_{b}"] for b in range(4)]
        exp += [(i % 16) >> b & 1 for b in range(4)]
    assert simulate(tmp_path, data, "opt", ops) == exp
