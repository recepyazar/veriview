"""Tests for state machine extraction (fsm.py), `--fsm` and the FSM data in the viewer."""

import json
import re

import pytest
from conftest import EX, needs_tools

from veriview.cli import main
from veriview.fsm import _cond, _minimize


def test_minimize_sync_reset_pattern():
    # inputs (LSB first): [rst, din];  (!rst && !din) || rst  ==  rst || !din
    cubes = _minimize(["00", "1-"], 2)
    assert sorted(cubes) == sorted(["1-", "-0"])
    assert _cond(cubes, ["rst", "din"]) in ("rst || !din", "!din || rst")


def test_minimize_everything_and_labels():
    assert _minimize(["0-", "1-"], 2) == ["--"]
    assert _cond(["--"], ["a", "b"]) == "1"
    assert _cond(["01"], ["a", "x == 3"]) == "!a && x == 3"
    assert _cond(["10"], ["a", "x == 3"]) == "a && !(x == 3)"


def _fsms(capsys, *args):
    assert main(["-q", *map(str, args), "--fsm"]) == 0
    return capsys.readouterr().out


@needs_tools
def test_traffic_fsm_report(capsys):
    out = _fsms(capsys, EX / "fsm" / "traffic.sv")
    assert "FSM traffic.state  (4 states, 2-bit state register, 8 transitions)" in out
    # enum names, reset state (*) and Moore outputs
    assert re.search(r"\* MAIN_GO\s+2'b00\s+main_green=1, main_yellow=0", out)
    for name in ("MAIN_YELLOW", "SIDE_GO", "SIDE_YELLOW"):
        assert name in out
    # conditions are the original source expressions
    assert "MAIN_GO -> MAIN_YELLOW  [car && timer >= 3'd4]" in out
    assert "SIDE_GO -> SIDE_YELLOW  [!car || timer == 3'd5]" in out
    assert "MAIN_YELLOW -> SIDE_GO  [timer == 3'd1]" in out
    assert "MAIN_GO -> MAIN_GO  [!(car && timer >= 3'd4)]" in out


@needs_tools
def test_mealy_fsm_localparams_and_sync_reset(capsys):
    out = _fsms(capsys, EX / "fsm" / "seq_fsm.v")
    assert "FSM seq_fsm.state  (4 states" in out
    assert re.search(r"\* IDLE\s+2'b00", out)                     # names from localparams
    assert re.search(r"GOT101\s+2'b11", out)
    # merged and simplified: (!rst && !din) || rst  ->  !din || rst
    assert re.search(r"IDLE -> IDLE  \[(!din \|\| rst|rst \|\| !din)\]", out)
    assert "GOT101 -> GOT1  [din && !rst]" in out   # terms sorted by signal name
    assert "GOT1 -> IDLE  [rst]" in out


@needs_tools
def test_no_fsm(capsys):
    assert "No state machines found." in _fsms(capsys, EX / "counter.sv")


@needs_tools
def test_fsm_dot_output(tmp_path, capsys):
    dot = tmp_path / "t.dot"
    assert main(["-q", str(EX / "fsm" / "traffic.sv"), "--fsm", "-o", str(dot)]) == 0
    text = dot.read_text()
    assert text.startswith('digraph "traffic.state"')
    assert 'shape=doublecircle, label="MAIN_GO\\nmain_green=1"' in text
    assert "label=\"car && timer >= 3'd4\"" in text


@needs_tools
def test_fsm_in_viewer(tmp_path):
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "fsm" / "traffic.sv"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    text = out.read_text()
    data = json.loads(re.search(r"const DATA = (.*?);\n", text).group(1))
    f = data["fsms"]["traffic"][0]
    assert f["name"] == "state" and f["reset"] == 0
    assert [s["name"] for s in f["states"]][0] == "MAIN_GO"
    assert {s["value"] for s in f["states"]} == {0, 1, 2, 3}
    assert any(i["expr"] == "car && timer >= 3'd4" and i["src"].endswith("traffic.sv:39.24-39.44") for i in f["inputs"])
    # the simulator finds the state register by name in the hierarchical netlist
    assert "state" in data["views"]["opt"]["modules"]["traffic"]["hier"]["net"]["names"]
    assert 'id="btn-fsm"' in text


@pytest.mark.parametrize("extra", [["--html"], ["--timing", "2ns"]])
@needs_tools
def test_fsm_option_conflicts(tmp_path, capsys, extra):
    assert main(["-q", str(EX / "fsm" / "traffic.sv"), "--fsm", *extra, "-o", str(tmp_path / "x")]) == 1
    assert "cannot be combined" in capsys.readouterr().err


@needs_tools
def test_several_fsms_and_reset_index(capsys):
    out = _fsms(capsys, EX / "fsm" / "fsm.f", "-t", "multi_fsm")
    heads = re.findall(r"^FSM (\S+)", out, re.M)
    assert sorted(heads) == ["multi_fsm.hs", "multi_fsm.ph", "seq_fsm.state", "traffic.state"]
    # hs is skipped by fsm_detect's recoding heuristic and found by the second pass
    assert "H_REQ -> H_WAIT  [1]" in out and "H_WAIT -> H_IDLE  [ack]" in out
    # STATE_RST is an index: the one-hot sequencer resets to P_A (3'b001), not to state #1
    assert re.search(r"\* P_A\s+3'b001", out)
    assert not re.search(r"\* P_B", out)


@needs_tools
def test_several_fsms_in_viewer(tmp_path):
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "fsm" / "fsm.f"), "-t", "multi_fsm", "--html", "--html-stages", "opt",
                 "-o", str(out)]) == 0
    data = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))
    assert {m: [f["name"] for f in fs] for m, fs in data["fsms"].items()} == {
        "multi_fsm": ["hs", "ph"], "seq_fsm": ["state"], "traffic": ["state"]}
    ph = next(f for f in data["fsms"]["multi_fsm"] if f["name"] == "ph")
    assert ph["states"][ph["reset"]]["name"] == "P_A"
