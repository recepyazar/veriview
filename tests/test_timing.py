"""Tests for the static timing engine (src/veriview/timing.js) and `--timing`."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import EX, needs_tools

from veriview.cli import main

ROOT = Path(__file__).resolve().parent.parent
TIMING_JS = ROOT / "src" / "veriview" / "timing.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node")


def run_js(tmp_path: Path, code: str, data=None):
    script = tmp_path / "t.js"
    script.write_text(f"const T = require({json.dumps(str(TIMING_JS))});\n"
                      f"const DATA = {json.dumps(data)};\n{code}")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_parse_time(tmp_path):
    res = run_js(tmp_path, """console.log(JSON.stringify(
      ["2ns", "500ps", "1.5", "250MHz", "1GHz", "0.5 ns", "x"].map((t) => T.parseTime(t))));""")
    assert res == [2000, 500, 1500, 4000, 1000, 500, None]


# A hand-made netlist: in -> NOT -> NOT -> FF -> AND(with in) -> out
#   bits: 2=in, 3=n1, 4=n2, 5=q, 6=out, 7=clk
CHAIN = {
    "cells": {
        "inv1": ["$_NOT_", {"A": ["input", [2]], "Y": ["output", [3]]}],
        "inv2": ["$_NOT_", {"A": ["input", [3]], "Y": ["output", [4]]}],
        "ff": ["$_DFF_P_", {"C": ["input", [7]], "D": ["input", [4]], "Q": ["output", [5]]}],
        "and": ["$_AND_", {"A": ["input", [5]], "B": ["input", [2]], "Y": ["output", [6]]}],
    },
    "ports": {"in": ["input", [2]], "clk": ["input", [7]], "out": ["output", [6]]},
    "names": {"in": [2], "n1": [3], "n2": [4], "q": [5], "out": [6], "clk": [7]},
}


def test_chain_numbers(tmp_path):
    res = run_js(tmp_path, """
      const r = T.analyzeTiming(DATA, { period: 1000, inputDelay: 100, uncertainty: 50 });
      console.log(JSON.stringify({
        d: Object.fromEntries(r.delay),
        eps: r.endpoints.map((e) => [e.name, e.arrival, e.minArrival, e.slack, e.holdSlack]),
        path: r.path(r.endpoints.find((e) => e.kind === "ff")).map((s) => [s.kind, s.net || s.name, s.arrival]),
        area: r.area, minPeriod: r.minPeriod, hold: r.worstHold }));""", CHAIN)
    # NOT: 15 + 5*fanout(1); AND: 40 + 5*1; FF clk->Q: 120 + 5*1
    assert res["d"] == {"inv1": 20, "inv2": 20, "ff": 125, "and": 45}
    eps = {e[0]: e[1:] for e in res["eps"]}
    # D arrives 100 + 20 + 20 = 140: setup slack 1000-80-50-140 = 730, hold slack 140-30-50 = 60
    assert eps["q (D)"] == [140, 140, 730, 60]
    # out: max(q 125, in 100) + 45 = 170; required 1000 - 0 - 50
    assert eps["out"] == [170, 145, 780, None]
    assert res["path"] == [["port", "in", 100], ["cell", "n1", 120], ["cell", "n2", 140]]
    assert res["minPeriod"] == 220  # 140 + setup 80
    assert res["hold"] == 60
    assert res["area"] == pytest.approx(3.75 * 2 + 20.02 + 6.26)


def test_combinational_loop_is_reported(tmp_path):
    loop = {
        "cells": {"a": ["$_NOT_", {"A": ["input", [3]], "Y": ["output", [2]]}],
                  "b": ["$_NOT_", {"A": ["input", [2]], "Y": ["output", [3]]}]},
        "ports": {"y": ["output", [3]]}, "names": {"y": [3]},
    }
    res = run_js(tmp_path, "console.log(JSON.stringify(T.analyzeTiming(DATA, {}).loopCells.sort()));", loop)
    assert res == ["a", "b"]


@needs_tools
def test_cli_timing_report_counter(tmp_path, capsys):
    assert main(["-q", str(EX / "counter.sv"), "--timing", "2ns", "--input-delay", "0ps"]) == 0
    out = capsys.readouterr().out
    assert "Timing report: counter" in out
    assert "Area            140.1 um^2 (10 cells)" in out
    # DFF(140) -> AND(50) -> NAND(27) -> XNOR(63) = 280 ps, + setup 80 = 360 ps
    assert "Critical path   360 ps" in out
    assert "count[3] (D)" in out and "VIOLATED" not in out
    assert "worst 130 ps" in out  # count[0]: clk->Q 140 + NOT 20 - hold 30


@needs_tools
def test_cli_timing_report_violations(tmp_path, capsys):
    assert main(["-q", str(EX / "pipe_mac.sv"), "--timing", "600ps", "--uncertainty", "200ps"]) == 0
    out = capsys.readouterr().out
    # exact gate structure of the multiplier depends on the Yosys/ABC version: check behaviour
    import re
    wns = int(re.search(r"Setup +WNS (-?\d+) ps", out).group(1))
    nviol = int(re.search(r"Setup .*?(\d+) violation", out).group(1))
    assert wns < -300 and nviol > 5
    assert re.search(r"Critical \(setup\) path to prod_r\[\d\] \(D\)", out)
    # input -> register paths: 100 ps input delay - 30 ps hold - 200 ps uncertainty
    assert "worst -130 ps" in out and re.search(r"Hold .*?[1-9]\d* violation", out)
    assert "required (hold + uncertainty)" in out


@needs_tools
def test_cli_timing_rca_ripple_path(tmp_path, capsys):
    assert main(["-q", str(EX / "rca" / "rca.f"), "--timing", "1ns", "--input-delay", "0"]) == 0
    out = capsys.readouterr().out
    assert "Critical path   866 ps" in out
    # the ripple-carry chain: through every full adder's carry
    assert all(f"fa_block[{i}].fa_inst.cout" in out for i in range(7))


@needs_tools
def test_timing_engine_in_viewer(tmp_path):
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "counter.sv"), "--html", "--html-stages", "gate", "-o", str(out)]) == 0
    text = out.read_text()
    assert "function analyzeTiming" in text and 'id="btn-timing"' in text


@pytest.mark.parametrize("args, msg", [
    (["--timing", "2ns", "--html"], "cannot be combined"),
    (["--timing", "fast"], "invalid period"),
])
@needs_tools
def test_cli_timing_errors(tmp_path, capsys, args, msg):
    assert main(["-q", str(EX / "counter.sv"), *args, "-o", str(tmp_path / "x.svg")]) == 1
    assert msg in capsys.readouterr().err
