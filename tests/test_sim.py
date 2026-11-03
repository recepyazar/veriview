"""Tests for the browser simulation engine (src/veriview/sim.js), run with node.

The viewer data is produced by the real pipeline (--html), then sim.js is driven
by a small node script: set inputs, clock, read port values (optionally of a
sub-instance).
"""

import json
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import EX, needs_sv, needs_tools

from veriview.cli import main

ROOT = Path(__file__).resolve().parent.parent
SIM_JS = ROOT / "src" / "veriview" / "sim.js"
NODE = shutil.which("node")

pytestmark = [needs_tools, pytest.mark.skipif(not NODE, reason="needs node")]

RUNNER = r"""
const S = require(process.argv[2]);
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const sc = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
const v = data.views[sc.stage], mods = v.modules;
const net = (m) => mods[m].hier.net;
const sim = S.createSimulator(net, (t) => !!mods[t]);
sim.top = v.top; sim.reset();
const modAt = (path) => path.reduce((m, inst) => mods[m].children[inst], v.top);
const out = [];
for (const [op, a, b] of sc.ops) {
  if (op === "set") sim.setInput(a, S.fromBig(BigInt(b), net(v.top).ports[a][1].length));
  else if (op === "clock") sim.clock(a, b);
  else if (op === "get") {
    const path = b || [];
    const bits = S.rd(sim.values(path.length ? "/" + path.join("/") : ""), net(modAt(path)).ports[a][1]);
    out.push(S.toBig(bits).toString());
  }
}
console.log(JSON.stringify({ out, unsupported: [...sim.unsupported], loop: sim.loopWarn,
  resetPorts: Object.fromEntries(sim.resetPorts), held: sim.held }));
"""


def build(tmp_path: Path, *args, stage: str = "opt") -> Path:
    out = tmp_path / "v.html"
    assert main(["-q", *map(str, args), "--html", "--stage", stage, "--html-stages", stage, "-o", str(out)]) == 0
    data = re.search(r"const DATA = (.*?);\n", out.read_text()).group(1)
    path = tmp_path / "data.json"
    path.write_text(data)
    return path


def simulate(tmp_path: Path, data: Path, stage: str, ops: list, full: bool = False):
    runner = tmp_path / "run.js"
    runner.write_text(RUNNER)
    sc = tmp_path / f"sc_{stage}.json"
    sc.write_text(json.dumps({"stage": stage, "ops": ops}))
    r = subprocess.run([NODE, str(runner), str(SIM_JS), str(data), str(sc)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    assert res["unsupported"] == [], res["unsupported"]
    assert not res["loop"]
    out = [int(x) for x in res["out"]]
    return (out, res) if full else out


STAGES = ["elab", "opt", "gate"]


@pytest.mark.parametrize("stage", STAGES)
def test_counter(tmp_path, stage):
    data = build(tmp_path, EX / "counter.sv", stage=stage)
    ops = [["set", "rst_n", 1], ["clock", "clk", 5], ["get", "count"], ["clock", "clk", 13], ["get", "count"],
           ["set", "rst_n", 0], ["get", "count"]]  # async reset acts immediately
    assert simulate(tmp_path, data, stage, ops) == [5, 2, 0]


@pytest.mark.parametrize("stage", STAGES)
def test_rca_and_subinstance(tmp_path, stage):
    data = build(tmp_path, EX / "rca" / "rca.f", stage=stage)
    rng = random.Random(1)
    ops, exp = [], []
    for _ in range(20):
        a, b, c = rng.randrange(256), rng.randrange(256), rng.randrange(2)
        ops += [["set", "a", a], ["set", "b", b], ["set", "cin", c], ["get", "s"], ["get", "cout"],
                ["get", "cin", ["fa_block[7].fa_inst"]]]
        total = a + b + c
        exp += [total & 0xFF, total >> 8, (((a & 0x7F) + (b & 0x7F) + c) >> 7) & 1]
    assert simulate(tmp_path, data, stage, ops) == exp


@pytest.mark.parametrize("stage", ["opt", "gate"])
def test_seqdect(tmp_path, stage):
    data = build(tmp_path, EX / "seqdect.sv", stage=stage)
    ops = [["set", "rst_n", 1]]
    for bit in [1, 0, 1, 1, 0]:
        ops += [["set", "i_serial", bit], ["clock", "clk", 1], ["get", "o_detected"]]
    assert simulate(tmp_path, data, stage, ops) == [0, 0, 0, 1, 0]


@needs_sv
def test_shift_reg_unpacked(tmp_path):
    data = build(tmp_path, EX / "shift_reg.sv")
    ops = [["set", "rst_n", 1], ["set", "en", 1], ["set", "d", 3], ["clock", "clk", 1], ["get", "q"],
           ["set", "d", 9], ["clock", "clk", 1], ["get", "q"],
           ["set", "en", 0], ["set", "d", 15], ["clock", "clk", 1], ["get", "q"]]
    # sv2v packs q[0] into the upper nibble
    assert simulate(tmp_path, data, "opt", ops) == [0x30, 0x93, 0x93]


OPS_V = """
module ops(input [7:0] a, input [7:0] b, input [2:0] sh, input s,
           input signed [7:0] sa, input signed [7:0] sb,
           output [7:0] y_add, output [7:0] y_sub, output [15:0] y_mul,
           output [7:0] y_and, output [7:0] y_or, output [7:0] y_xor, output [7:0] y_not,
           output [7:0] y_shl, output [7:0] y_shr, output [7:0] y_sshr, output [7:0] y_mux,
           output y_eq, output y_lt, output y_slt, output y_ge,
           output y_rand, output y_ror, output y_rxor, output y_land, output y_lnot,
           output [7:0] y_div, output [7:0] y_mod, output [7:0] y_neg, output [7:0] y_case);
  assign y_add = a + b;   assign y_sub = a - b;   assign y_mul = a * b;
  assign y_and = a & b;   assign y_or = a | b;    assign y_xor = a ^ b;   assign y_not = ~a;
  assign y_shl = a << sh; assign y_shr = a >> sh; assign y_sshr = sa >>> sh;
  assign y_mux = s ? a : b;
  assign y_eq = a == b;   assign y_lt = a < b;    assign y_slt = sa < sb;  assign y_ge = a >= b;
  assign y_rand = &a;     assign y_ror = |b;      assign y_rxor = ^a;
  assign y_land = a && b; assign y_lnot = !a;
  assign y_div = (b != 0) ? a / b : 8'd0;
  assign y_mod = (b != 0) ? a % b : 8'd0;
  assign y_neg = -a;
  reg [7:0] c;
  always @* case (sh)
    3'd0: c = a; 3'd1: c = b; 3'd2: c = a + 1; 3'd5: c = 8'hA5; default: c = 8'h00;
  endcase
  assign y_case = c;
endmodule
"""


def _expected(a, b, sh, s, sa, sb):
    def sgn(x):
        return x - 256 if x & 0x80 else x
    m8 = 0xFF
    case = {0: a, 1: b, 2: (a + 1) & m8, 5: 0xA5}.get(sh, 0)
    return {
        "y_add": (a + b) & m8, "y_sub": (a - b) & m8, "y_mul": a * b,
        "y_and": a & b, "y_or": a | b, "y_xor": a ^ b, "y_not": ~a & m8,
        "y_shl": (a << sh) & m8, "y_shr": a >> sh, "y_sshr": (sgn(sa) >> sh) & m8,
        "y_mux": a if s else b,
        "y_eq": int(a == b), "y_lt": int(a < b), "y_slt": int(sgn(sa) < sgn(sb)), "y_ge": int(a >= b),
        "y_rand": int(a == 0xFF), "y_ror": int(b != 0), "y_rxor": bin(a).count("1") & 1,
        "y_land": int(bool(a) and bool(b)), "y_lnot": int(a == 0),
        "y_div": a // b if b else 0, "y_mod": a % b if b else 0, "y_neg": -a & m8, "y_case": case,
    }


@pytest.mark.parametrize("stage", STAGES)
def test_operators_random(tmp_path, stage):
    src = tmp_path / "ops.v"
    src.write_text(OPS_V)
    data = build(tmp_path, src, stage=stage)
    rng = random.Random(42)
    ops, exp = [], []
    vectors = [(0, 0, 0, 0, 0, 0), (255, 255, 7, 1, 128, 127)] + [
        tuple(rng.randrange(n) for n in (256, 256, 8, 2, 256, 256)) for _ in range(25)]
    for a, b, sh, s, sa, sb in vectors:
        ops += [["set", k, v] for k, v in zip(("a", "b", "sh", "s", "sa", "sb"), (a, b, sh, s, sa, sb))]
        e = _expected(a, b, sh, s, sa, sb)
        for name, val in e.items():
            ops.append(["get", name])
            exp.append(val)
    got = simulate(tmp_path, data, stage, ops)
    names = [op[1] for op in ops if op[0] == "get"]
    mism = [(n, g, e) for n, g, e in zip(names, got, exp) if g != e]
    assert not mism, mism[:10]


WRAP_V = """
module top(input clk, input rst_n, output [3:0] c0, output [3:0] c1);
  counter u0 (.clk(clk), .rst_n(rst_n), .count(c0));
  counter u1 (.clk(clk), .rst_n(rst_n), .count(c1));
endmodule
"""


@pytest.mark.parametrize("stage", ["opt", "gate"])
def test_reset_inputs_default_inactive(tmp_path, stage):
    """Active-low resets start released, so clocking works without touching rst_n."""
    data = build(tmp_path, EX / "counter.sv", stage=stage)
    out, res = simulate(tmp_path, data, stage, [["clock", "clk", 3], ["get", "count"]], full=True)
    assert out == [3]
    assert res["resetPorts"] == {"rst_n": 1} and res["held"] == []


def test_reset_traced_through_hierarchy(tmp_path):
    (tmp_path / "top.v").write_text(WRAP_V)
    data = build(tmp_path, tmp_path / "top.v", EX / "counter.sv", "-t", "top")
    ops = [["clock", "clk", 2], ["get", "c0"], ["get", "count", ["u1"]],
           ["set", "rst_n", 0], ["get", "c1"]]
    out, res = simulate(tmp_path, data, "opt", ops, full=True)
    assert out == [2, 2, 0]
    assert res["resetPorts"] == {"rst_n": 1}
    # both sub-module counters are reported as held by the top-level rst_n
    assert res["held"] == [{"port": "rst_n", "level": 0, "count": 2}]


WAVE_RUNNER = r"""
const S = require(process.argv[2]);
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const v = data.views.opt, mods = v.modules, net = mods[v.top].hier.net;
const sim = S.createSimulator((m) => mods[m].hier.net, (t) => !!mods[t]);
sim.top = v.top; sim.reset();
const bits = [1, 0, 1, 1, 0, 1, 1, 0];
const steps = bits.map((b) => ({ i_serial: [b], rst_n: [1] }));
const probes = { o_detected: net.names.o_detected, shift_reg: net.names.shift_reg };
const rec = sim.runWave("clk", steps, probes);
sim.runWave("clk", steps, {}, 4);  // leave the design in cycle 4
const at4 = S.toBig(S.rd(sim.values(""), net.names.shift_reg)).toString();
const vcd = S.toVCD([{ name: "i_serial", values: bits.map((b) => [b]) },
                     { name: "shift_reg", values: rec.shift_reg }], { clkName: "clk", top: v.top });
console.log(JSON.stringify({
  det: rec.o_detected.map((x) => x[0]).join(""),
  sr: rec.shift_reg.map((b) => S.toBig(b).toString(16)).join(","),
  at4, cycle: sim.cycle, vcd,
}));
"""


def test_waveform_and_vcd(tmp_path):
    data = build(tmp_path, EX / "seqdect.sv")
    runner = tmp_path / "wave.js"
    runner.write_text(WAVE_RUNNER)
    r = subprocess.run([NODE, str(runner), str(SIM_JS), str(data)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    res = json.loads(r.stdout)
    # inputs of cycle t are clocked in at the start of cycle t+1: detections at 4 and 7 (overlap)
    assert res["det"] == "00001001"
    assert res["sr"] == "0,1,2,5,b,6,d,b"
    assert res["at4"] == "11" and res["cycle"] == 4
    vcd = res["vcd"].splitlines()
    assert "$timescale 1ns $end" in vcd and "$enddefinitions $end" in vcd
    assert "$var wire 1 ! clk $end" in vcd and '$var wire 4 # shift_reg [3:0] $end' in vcd
    # cycle 4 starts at #40 with a rising clock edge and shift_reg = 1011
    i = vcd.index("#40")
    assert vcd[i + 1] == "1!" and "b1011 #" in vcd[i + 1:i + 4]
