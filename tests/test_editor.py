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
