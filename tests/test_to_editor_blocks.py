"""'Open in editor' with blocks: the drawing made from a module must behave like the module.

Each design is synthesized for the editor's blocks (stage editor-blocks), converted to a drawing
(to_editor.netlist_to_blocks), turned back into Verilog by the editor (editor_core.js), and both
the original and the redrawn design are simulated with the same random stimulus.
"""

from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import pytest
from conftest import EX, needs_tools
from test_editor import to_verilog
from test_sim import build, simulate

from veriview.filelist import collect_sources
from veriview.synth import Synthesizer
from veriview.to_editor import netlist_to_blocks

pytestmark = [needs_tools, pytest.mark.skipif(not shutil.which("node"), reason="needs node")]

DESIGNS = [  # (sources, top, extra sources needed by the redrawn design)
    (["counter.sv"], "counter", []),
    (["fsm/traffic.sv"], "traffic", []),
    (["seqdect.sv"], "seqdect", []),
    (["rca/rca.f"], "rca", ["rca/fa.sv", "rca/ha.sv"]),
    (["pipe_mac.sv"], "pipe_mac", []),
    (["mux/mux.sv"], None, []),
]


def to_blocks(tmp_path: Path, sources: list[str], top: str | None) -> dict:
    out = tmp_path / "blocks.json"
    Synthesizer(collect_sources([str(EX / s) for s in sources], [], [], resolve=False), tmp_path).synthesize(
        top, out, stage="editor-blocks")
    return netlist_to_blocks(json.loads(out.read_text()))


@pytest.mark.parametrize("sources, top, extra", DESIGNS, ids=[d[1] or "mux" for d in DESIGNS])
def test_redrawn_design_behaves_the_same(tmp_path, sources, top, extra):
    d = to_blocks(tmp_path, sources, top)
    res = to_verilog(tmp_path, d, d["module"])
    assert res["warn"] == [], res["warn"]
    redrawn = tmp_path / f"{d['module']}_drawn.v"
    redrawn.write_text(res["text"])
    ins = {n["name"]: n.get("bits", 1) for n in d["nodes"] if n["type"] == "in"}
    outs = [n["name"] for n in d["nodes"] if n["type"] == "out"]
    rng = random.Random(7)
    ops = []
    for _ in range(40):
        for p, w in ins.items():
            if p != "clk":
                ops.append(["set", p, rng.randrange(1 << w)])
        if "clk" in ins:
            ops.append(["clock", "clk", 1])
        ops += [["get", o] for o in outs]
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    original = simulate(tmp_path / "a", build(tmp_path / "a", *[EX / s for s in sources]), "opt", ops)
    again = simulate(tmp_path / "b", build(tmp_path / "b", redrawn, *[EX / s for s in extra]), "opt", ops)
    assert again == original


def test_counter_is_drawn_with_blocks(tmp_path):
    d = to_blocks(tmp_path, ["counter.sv"], "counter")
    types = sorted(n["type"] for n in d["nodes"])
    assert types == ["add", "const", "dffr", "in", "in", "out"]            # count + 1, register, ports
    reg = next(n for n in d["nodes"] if n["type"] == "dffr")
    assert reg["bits"] == 4 and "label" not in reg     # "count" is the output port's name already


def test_sub_modules_stay_module_blocks(tmp_path):
    d = to_blocks(tmp_path, ["rca/rca.f"], "rca")
    insts = [n for n in d["nodes"] if n["type"] == "inst"]
    assert len(insts) == 8 and {n["module"] for n in insts} == {"fa"}
    assert [p["name"] for p in insts[0]["ports"]] == ["a", "b", "cin", "s", "cout"]
    assert insts[0]["file"].endswith("fa.sv")
