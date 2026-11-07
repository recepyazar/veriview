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


def design_scenario():
    ops, exp = [["set", "rst_n", 1]], []
    for a, b, cin, sel in itertools.product((0, 1), repeat=4):
        ops += [["set", "a", a], ["set", "b", b], ["set", "cin", cin], ["set", "sel", sel],
                ["get", "sum"], ["get", "cout"], ["get", "nand3"], ["get", "m"]]
        exp += [a ^ b ^ cin, (a & b) | ((a ^ b) & cin), 1 - (a & b & cin), b if sel else a]
    # the register captures the sum on the clock edge; the asynchronous reset clears it
    ops += [["set", "a", 1], ["set", "b", 0], ["set", "cin", 0], ["clock", "clk", 1], ["get", "q"],
            ["set", "rst_n", 0], ["get", "q"]]
    exp += [1, 0]
    return ops, exp


@needs_tools
@pytest.mark.parametrize("stage", ["opt", "gate"])
def test_drawn_design_simulates_correctly(tmp_path, stage):
    src = tmp_path / "drawn.v"
    src.write_text(to_verilog(tmp_path, DESIGN)["text"])
    data = build(tmp_path, src, stage=stage)
    ops, exp = design_scenario()
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
    assert [n["name"] for n in d["nodes"] if n["type"] == "out"] == ["count"]     # a 4-bit output fed by a merger
    ffs = [n for n in d["nodes"] if n["type"] in ("dff", "dffr")]
    assert {n.get("label") for n in ffs} == {f"count_{i}" for i in range(4)}
    assert len({(n["x"], n["y"]) for n in d["nodes"]}) == len(d["nodes"])       # no two nodes on top of each other
    res = to_verilog(tmp_path, d, d["module"])
    assert res["warn"] == []
    src = tmp_path / "counter_gates.v"
    src.write_text(res["text"])
    data = build(tmp_path, src, stage="opt")
    ops = [["set", "rst_n", 1]]
    exp = []
    for i in range(1, 19):
        ops += [["clock", "clk", 1], ["get", "count"]]
        exp += [i % 16]
    assert simulate(tmp_path, data, "opt", ops) == exp


# buses and blocks: 4-bit a, b; adder, subtractor, comparator, 4:1 MUX of constants, decoder,
# split/merge (bit reversal), a 4-bit AND gate and an output whose width follows its driver,
# a register with enable + reset and a counter
OUTS = ["s", "cout", "d", "bout", "eq", "lt", "gt", "m", "onehot", "rev", "ab", "q", "c"]
BLOCKS = {
    "nodes": [
        node(1, "in", name="a", bits=4), node(2, "in", name="b", bits=4), node(3, "in", name="cin"),
        node(4, "in", name="sel", bits=2), node(5, "in", name="clk"), node(6, "in", name="rst_n"),
        node(7, "in", name="en"), node(10, "add", bits=4), node(11, "sub", bits=4), node(12, "cmp", bits=4),
        node(13, "muxn", bits=4, k=4), node(14, "const", bits=4, value=3), node(15, "const", bits=4, value=9),
        node(16, "const", bits=4, value=12), node(17, "const", bits=4, value=15), node(18, "dec", bits=2),
        node(19, "split", bits=4), node(20, "merge", bits=4), node(21, "and", n=2), node(22, "reg", bits=4),
        node(23, "cnt", bits=3), *[node(30 + i, "out", name=n) for i, n in enumerate(OUTS)],
    ],
    "wires": [
        W(1, "Y", 10, "A"), W(2, "Y", 10, "B"), W(3, "Y", 10, "CIN"), W(10, "S", 30, "A"), W(10, "COUT", 31, "A"),
        W(1, "Y", 11, "A"), W(2, "Y", 11, "B"), W(11, "Y", 32, "A"), W(11, "BOUT", 33, "A"),
        W(1, "Y", 12, "A"), W(2, "Y", 12, "B"), W(12, "EQ", 34, "A"), W(12, "LT", 35, "A"), W(12, "GT", 36, "A"),
        W(14, "Y", 13, "D0"), W(15, "Y", 13, "D1"), W(16, "Y", 13, "D2"), W(17, "Y", 13, "D3"), W(4, "Y", 13, "S"),
        W(13, "Y", 37, "A"), W(4, "Y", 18, "A"), W(18, "Y", 38, "A"),
        W(1, "Y", 19, "A"), W(19, "B0", 20, "B3"), W(19, "B1", 20, "B2"), W(19, "B2", 20, "B1"), W(19, "B3", 20, "B0"),
        W(20, "Y", 39, "A"), W(1, "Y", 21, "A"), W(2, "Y", 21, "B"), W(21, "Y", 40, "A"),
        W(10, "S", 22, "D"), W(5, "Y", 22, "CLK"), W(7, "Y", 22, "EN"), W(6, "Y", 22, "RST_N"), W(22, "Q", 41, "A"),
        W(5, "Y", 23, "CLK"), W(23, "Q", 42, "A"),
    ],
}


def test_bus_verilog_text(tmp_path):
    res = to_verilog(tmp_path, BLOCKS, "blocks")
    text = res["text"]
    assert res["warn"] == []
    assert "input  wire [3:0] a," in text and "output wire [7:0] onehot" not in text
    assert "output wire [3:0] onehot," in text and "output wire [3:0] ab," in text and "output wire [2:0] c" in text
    assert "assign {add10_cout, add10} = a + b + cin;" in text
    assert "assign {sub11_bout, sub11} = {1'b0, a} - {1'b0, b};" in text
    assert "assign muxn13 = (sel == 2'd0) ? 4'd3 : (sel == 2'd1) ? 4'd9 : (sel == 2'd2) ? 4'd12 : 4'd15;" in text
    assert "assign dec18 = 4'd1 << sel;" in text
    assert "assign merge20 = {split19[0], split19[1], split19[2], split19[3]};" in text
    assert "if (!rst_n) reg22 <= 4'd0;" in text and "else if (en) reg22 <= add10;" in text
    assert "always @(posedge clk)\n    cnt23 <= cnt23 + 1'b1;" in text      # no reset / enable connected


def test_bus_width_warnings(tmp_path):
    d = {"nodes": [node(1, "in", name="a", bits=4), node(2, "in", name="b", bits=2), node(3, "add", bits=4),
                   node(4, "out", name="y", bits=8)],
         "wires": [W(1, "Y", 3, "A"), W(2, "Y", 3, "B"), W(3, "S", 4, "A")]}
    warn = " | ".join(to_verilog(tmp_path, d)["warn"])
    assert "width mismatch: 2 bits into B of ADD3 (4 bits)" in warn
    assert "width mismatch: 4 bits into A of y (8 bits)" in warn
    assert "CIN" not in warn                                                # optional pins may stay open


def blocks_scenario():
    ops, exp = [["set", "rst_n", 1], ["set", "en", 0]], []
    consts = [3, 9, 12, 15]
    for a, b, cin, sel in [(5, 3, 0, 0), (9, 9, 1, 1), (2, 14, 1, 2), (15, 15, 1, 3), (0, 1, 0, 1)]:
        ops += [["set", "a", a], ["set", "b", b], ["set", "cin", cin], ["set", "sel", sel]]
        ops += [["get", x] for x in ("s", "cout", "d", "bout", "eq", "lt", "gt", "m", "onehot", "rev", "ab")]
        rev = int(f"{a:04b}"[::-1], 2)
        exp += [(a + b + cin) & 15, (a + b + cin) >> 4, (a - b) & 15, int(a < b), int(a == b), int(a < b), int(a > b),
                consts[sel], 1 << sel, rev, a & b]
    # register: holds while en = 0, loads the sum with en = 1; async reset clears it; the counter counts
    ops += [["set", "a", 6], ["set", "b", 1], ["set", "cin", 0], ["clock", "clk", 1], ["get", "q"],
            ["set", "en", 1], ["clock", "clk", 1], ["get", "q"], ["set", "rst_n", 0], ["get", "q"], ["get", "c"],
            ["clock", "clk", 4], ["get", "c"]]
    exp += [0, 7, 0, 2, 6]   # 2 clocks so far -> c = 2; 4 more -> 6
    return ops, exp


@needs_tools
def test_bus_design_simulates(tmp_path):
    src = tmp_path / "blocks.v"
    src.write_text(to_verilog(tmp_path, BLOCKS, "blocks")["text"])
    data = build(tmp_path, src, stage="opt")
    ops, exp = blocks_scenario()
    assert simulate(tmp_path, data, "opt", ops) == exp


# ---- the editor's own simulator (createSim): same scenarios, same results as the compiled Verilog ----
def editor_sim(tmp_path: Path, design: dict, ops: list) -> list:
    script = tmp_path / "esim.js"
    script.write_text(f"""const E = require({json.dumps(str(CORE))});
const D = {json.dumps(design)}, ops = {json.dumps(ops)};
const id = (name) => D.nodes.find((n) => (n.type === "in" || n.type === "out") && n.name === name).id;
const s = E.createSim(D), res = [];
for (const [op, name, v] of ops) {{
  if (op === "set") s.setInput(id(name), BigInt(v));
  else if (op === "clock") s.clock(id(name), v);
  else res.push(Number(s.value(...(() => {{ const o = D.nodes.find((n) => n.id === id(name));
    const w = D.wires.find((x) => x.to.node === o.id); return [w.from.node, w.from.pin]; }})())));
}}
console.log(JSON.stringify(res));""")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("design, scenario", [(DESIGN, design_scenario), (BLOCKS, blocks_scenario)])
def test_editor_simulator(tmp_path, design, scenario):
    ops, exp = scenario()
    assert editor_sim(tmp_path, design, ops) == exp


def test_editor_simulator_labels_and_derived_clock(tmp_path):
    # a register clocked through a net label and an AND gate (clock gating): counts only while en = 1
    d = {"nodes": [node(1, "in", name="clk"), node(2, "in", name="en"), node(3, "and"), node(4, "tag", name="gclk"),
                   node(5, "tag", name="gclk"), node(6, "cnt", bits=4), node(7, "out", name="q")],
         "wires": [W(1, "Y", 3, "A"), W(2, "Y", 3, "B"), W(3, "Y", 4, "T"), W(5, "T", 6, "CLK"), W(6, "Q", 7, "A")]}
    ops = [["set", "en", 1], ["clock", "clk", 3], ["get", "q"], ["set", "en", 0], ["clock", "clk", 5], ["get", "q"]]
    assert editor_sim(tmp_path, d, ops) == [3, 3]
