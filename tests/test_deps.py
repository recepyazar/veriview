"""Finding the files a design uses but was not given (deps.py), and the error-handling helpers
that big SystemVerilog projects need (synth.py)."""

from pathlib import Path

from veriview import deps
from veriview.filelist import collect_sources
from veriview.synth import Synthesizer, summarize_error


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "rtl").mkdir()
    (root / "ip" / "src").mkdir(parents=True)
    (root / "ip" / "include" / "ip").mkdir(parents=True)
    (root / "tools" / "src" / "other").mkdir(parents=True)
    (root / "tools" / "src" / "other" / ".git").mkdir()
    (root / "rtl" / "top.sv").write_text(
        "module top(input logic clk, output logic [7:0] y);\n"
        "  import ip_pkg::*;\n"
        "  always_comb if (clk) y = 0; else y = 1;   // `if (` is not an instance of a module `i`\n"
        "  ip_core #(.W(8)) u_core (.clk(clk), .y());\n"
        "endmodule\n")
    (root / "ip" / "src" / "ip_pkg.sv").write_text("package ip_pkg;\n  localparam int W = 8;\nendpackage\n")
    (root / "ip" / "src" / "ip_core.sv").write_text(
        '`include "ip/regs.svh"\nmodule ip_core #(parameter W = 4) (input clk, output [W-1:0] y);\n'
        "  lzc u_lzc ();\nendmodule\n")
    (root / "ip" / "src" / "lzc.sv").write_text("module lzc; endmodule\n")
    (root / "ip" / "include" / "ip" / "regs.svh").write_text("`define FF(q, d) always @(posedge clk) q <= d;\n")
    # a tool's sources inside the project (its own git repository): never part of the design
    (root / "tools" / "src" / "other" / "i.v").write_text("module i; endmodule\nmodule lzc; endmodule\n")
    return root


def test_resolve_finds_packages_modules_and_includes(tmp_path):
    root = _project(tmp_path)
    added, incs, notes = deps.resolve([root / "rtl" / "top.sv"], [])
    names = sorted(f.name for f in added)
    assert names == ["ip_core.sv", "ip_pkg.sv", "lzc.sv"]
    assert all("tools" not in f.parts for f in added)                         # nested repositories skipped
    assert incs == [root / "ip" / "include"]                                  # the folder of "ip/regs.svh"
    assert any("package ip_pkg" in n for n in notes) and any("module lzc" in n for n in notes)


def test_collect_sources_adds_and_orders_packages_first(tmp_path, monkeypatch):
    root = _project(tmp_path)
    src = collect_sources([str(root / "rtl" / "top.sv")], [], [])
    assert src.files[0].name == "ip_pkg.sv"                                   # packages before their users
    assert {f.name for f in src.files} == {"top.sv", "ip_pkg.sv", "ip_core.sv", "lzc.sv"}
    monkeypatch.setenv("VERIVIEW_NO_DEPS", "1")
    assert [f.name for f in collect_sources([str(root / "rtl" / "top.sv")], [], []).files] == ["top.sv"]


def test_nothing_added_outside_a_project(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    (d / "a.v").write_text("module a; b u_b (); endmodule\n")
    (tmp_path / "b.v").write_text("module b; endmodule\n")   # not below a git work tree: not searched
    assert deps.resolve([d / "a.v"], []) == ([], [], [])


def test_summarize_error_drops_ast_dumps():
    noise = "\n".join(["{", '  "kind": "Call",', "}"] * 200)
    text = noise + "\nSource line rtl/x.sv:69:43:  q <= f(a);\nERROR: Feature unimplemented at slang.cc:1618\n"
    out = summarize_error(text)
    assert out.splitlines() == ["Source line rtl/x.sv:69:43:  q <= f(a);",
                                "ERROR: Feature unimplemented at slang.cc:1618"]


def test_module_at_and_roots(tmp_path):
    f = tmp_path / "m.sv"
    f.write_text("module a;\n  b u ();\nendmodule\n\nmodule b;\n  wire x;\nendmodule\n")
    s = Synthesizer(collect_sources([str(f)], [], [], resolve=False), tmp_path)
    assert s._module_at(str(f), 2) == "a" and s._module_at(str(f), 6) == "b" and s._module_at(str(f), 4) is None
    assert s._roots() == {"a"}                  # b is instantiated by a: only a is a root (never a black box)
    s._top = "b"
    assert s._roots() == {"b"}


def test_big_viewer_data_is_compressed(tmp_path, monkeypatch):
    """Above VERIVIEW_GZIP_MIN the data is embedded gzip+base64; the page's unpack code restores it."""
    import base64
    import gzip
    import json
    import re
    import shutil
    import subprocess

    import pytest
    from conftest import EX

    from veriview import html as vvhtml
    from veriview.cli import main
    if not (shutil.which("yosys") and shutil.which("netlistsvg") and shutil.which("node")):
        pytest.skip("needs yosys, netlistsvg and node")
    monkeypatch.setattr(vvhtml, "GZIP_MIN", 1000)
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "counter.sv"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    text = out.read_text()
    packed = json.loads(re.search(r"const DATA = (.*?);\n", text).group(1))
    data = json.loads(gzip.decompress(base64.b64decode(packed["gz"])))
    assert data["views"]["opt"]["top"] == "counter"
    # the browser side: the same unpacking code, run by node (DecompressionStream, Blob, Response, atob)
    script = text[text.index("const DATA = "):text.index("VV_DATA.then(")] + \
        "VV_DATA.then((d) => console.log(d.views.opt.top));"
    js = tmp_path / "unpack.js"
    js.write_text(script)
    r = subprocess.run([shutil.which("node"), str(js)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "counter", r.stderr


# ---- slang: designs that only partly synthesize --------------------------------------------------
def _slang_yosys():
    import os

    from veriview.tools import find_tool, slang_available
    for y in (os.environ.get("VERIVIEW_YOSYS"), find_tool("yosys")):
        if y and slang_available(y):
            return y
    return None


SIM_SOC = """
module dram_model(input logic clk, input logic [7:0] addr, output logic [63:0] q);
  import "DPI-C" function longint mem_read(input longint a);
  always_ff @(posedge clk) q <= mem_read(64'(addr));
endmodule
module core(input logic clk, input logic [63:0] d, output logic [7:0] a, output logic [63:0] y);
  logic [63:0] acc;
  always_ff @(posedge clk) acc <= acc + d;
  assign a = acc[7:0];
  assign y = acc;
  // a table built by a loop longer than slang's default unroll limit (4000)
  logic [7:0] tbl [0:4999];
  always_comb for (int i = 0; i < 5000; i++) tbl[i] = 8'(i);
endmodule
module soc(input logic clk, output logic [63:0] y);
  logic [7:0] a; logic [63:0] q; int seed;
  initial if (!$value$plusargs("seed=%d", seed)) seed = 0;
  core u_core(.clk(clk), .d(q), .a(a), .y(y));
  dram_model u_mem(.clk(clk), .addr(a), .q(q));
endmodule
"""


def test_slang_black_boxes_and_unroll(tmp_path, monkeypatch):
    import json

    import pytest
    y = _slang_yosys()
    if not y:
        pytest.skip("needs a Yosys with slang")
    from veriview.tools import slang_has_option
    if not slang_has_option(y, "--blackboxed-module"):
        pytest.skip("this yosys-slang has no --blackboxed-module")
    monkeypatch.setenv("VERIVIEW_YOSYS", y)
    f = tmp_path / "soc.sv"
    f.write_text(SIM_SOC)
    src = collect_sources([str(f)], [], [], resolve=False)
    # the core (with the long loop) synthesizes: the unroll limit is raised on demand
    s = Synthesizer(src, tmp_path, frontend="slang")
    s.synthesize("core", tmp_path / "core.json")
    assert s.unroll_limit and "core" in json.loads((tmp_path / "core.json").read_text())["modules"]
    # the SoC: the DPI model becomes a black box, but the top itself only works in simulation
    s = Synthesizer(src, tmp_path, frontend="slang")
    try:
        s.synthesize("soc", tmp_path / "soc.json")
        mods = json.loads((tmp_path / "soc.json").read_text())["modules"]
        assert "dram_model" in s.blackbox and any(n.startswith("core") for n in mods)
    except Exception as e:       # slang versions that reject $value$plusargs: a clear hint instead
        msg = str(e)
        assert "only works in simulation" in msg and "Try a synthesizable top module: core" in msg, msg


def test_error_locations_both_slang_formats():
    from veriview.synth import _ERR_LOC_RE
    text = ("rtl/soc.sv:17: ERROR: unsupported system task '$value$plusargs'\n"
            "rtl/soc.sv:54:9: error: unsupported system task '$test$plusargs'\n"
            "Source line rtl/dram.sv:69:43:   q <= f(a);\n"
            "rtl/x.sv:3:1: warning: not an error location\n")
    locs = [(m.group(1) or m.group(3), int(m.group(2) or m.group(4))) for m in _ERR_LOC_RE.finditer(text)]
    assert locs == [("rtl/soc.sv", 17), ("rtl/soc.sv", 54), ("rtl/dram.sv", 69)]


def test_killed_yosys_is_reported(tmp_path, monkeypatch):
    import os

    import pytest

    from veriview.log import VeriViewError
    if os.name != "posix":
        pytest.skip("POSIX signals")
    fake = tmp_path / "yosys"
    fake.write_text("#!/bin/sh\nkill -9 $$\n")
    fake.chmod(0o755)
    monkeypatch.setenv("VERIVIEW_YOSYS", str(fake))
    (tmp_path / "a.sv").write_text("module a; endmodule\n")
    s = Synthesizer(collect_sources([str(tmp_path / "a.sv")], [], [], resolve=False), tmp_path)
    with pytest.raises(VeriViewError, match="stopped by signal 9.*out of memory"):
        s.synthesize("a", tmp_path / "a.json")
    assert list(s.errors) == ["yosys"]          # not retried with other frontends
