"""Drawing big schematics: netlistsvg through netlistsvg_fast.js, size limits, failures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import EX, needs_tools

from veriview import html as vvhtml
from veriview.render import FAST_LOADER, _netlistsvg_script, fast_layout_skin, run_netlistsvg
from veriview.tools import find_tool

NODE = shutil.which("node")

# a byte-wide datapath sliced into many overlapping bit ranges: netlistsvg's split/join search
# (FlatModule.gather) recurses once per bit and scans every port, so wide versions of this
# overflow Node's stack and take quadratic time
WIDE = """
module wide #(parameter W = 192) (input [W-1:0] a, input [W-1:0] b, input [7:0] s, output [W-1:0] y, output [W-1:0] z);
  genvar i;
  generate for (i = 0; i < W / 8; i = i + 1) begin : g
    assign y[i*8 +: 8] = a[i*8 +: 8] ^ b[(W - 8 - i*8) +: 8];
    assign z[i*8 +: 8] = s[i % 8] ? a[i*8 +: 8] : b[i*8 +: 8];
  end endgenerate
endmodule
"""


def _netlist(tmp_path: Path, src: Path, top: str) -> Path:
    out = tmp_path / f"{top}.json"
    r = subprocess.run([find_tool("yosys"), "-q", "-p", f"read_verilog -sv {src}; hierarchy -top {top}; proc; opt; "
                        f"write_json {out}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out


@needs_tools
@pytest.mark.skipif(not NODE, reason="needs node")
@pytest.mark.parametrize("design", ["wide", "rca"])
def test_fast_loader_draws_exactly_the_same(tmp_path, design):
    netlistsvg = find_tool("netlistsvg")
    script = _netlistsvg_script(netlistsvg)
    if not script:
        pytest.skip("netlistsvg is not a node script here")
    if design == "wide":
        src = tmp_path / "wide.v"
        src.write_text(WIDE)
        js = _netlist(tmp_path, src, "wide")
    else:
        js = tmp_path / "rca.json"
        srcs = " ".join(str(EX / "rca" / f) for f in ("fa.sv", "ha.sv", "rca.sv"))
        subprocess.run([find_tool("yosys"), "-q", "-p", f"read_verilog -sv {srcs}; hierarchy -top rca; proc; flatten; "
                        f"opt; write_json {js}"], check=True)
    plain, fast = tmp_path / "plain.svg", tmp_path / "fast.svg"
    # the plain netlistsvg needs a big stack for this one; the fast loader does not
    subprocess.run(["sh", "-c", f'ulimit -s 550000; exec "{NODE}" --stack-size=500000 "{script}" "{js}" -o "{plain}"'],
                   check=True)
    subprocess.run([NODE, str(FAST_LOADER), str(script), str(js), "-o", str(fast)], check=True)
    assert plain.read_bytes() == fast.read_bytes()


@needs_tools
def test_fast_layout_skin_and_run(tmp_path):
    netlistsvg = find_tool("netlistsvg")
    skin = fast_layout_skin(netlistsvg, None)
    if skin is None:
        pytest.skip("no default skin found")
    text = skin.read_text()
    assert 'org.eclipse.elk.layered.thoroughness="1"' in text and "<s:layoutEngine" in text
    src = tmp_path / "wide.v"
    src.write_text(WIDE)
    svg = run_netlistsvg(_netlist(tmp_path, src, "wide"), tmp_path / "w.svg", fast=True, timeout=300)
    assert svg.lstrip().startswith("<svg") and "cell_" in svg


def test_flat_cells_and_placeholder():
    mods = {"top": {"cells": {"u1": {"type": "mid"}, "u2": {"type": "mid"}, "g": {"type": "$and"}}},
            "mid": {"cells": {"u": {"type": "leaf"}, "x": {"type": "$or"}}},
            "leaf": {"cells": {"a": {"type": "$not"}, "b": {"type": "$not"}}}}
    assert vvhtml.flat_cells(mods, "leaf") == 2 and vvhtml.flat_cells(mods, "mid") == 3
    assert vvhtml.flat_cells(mods, "top") == 7
    svg = vvhtml.placeholder_svg("big", 12345, "details <here>")
    assert 'data-i18n="Too large to draw: {n} cells."' in svg and 'data-n="12345"' in svg
    assert "Too large to draw: 12345 cells." in svg and "&lt;here&gt;" in svg


@needs_tools
def test_huge_views_are_skipped_not_fatal(tmp_path, monkeypatch):
    """A view above the cell limit gets a placeholder; a netlistsvg failure is not fatal either."""
    from veriview.cli import main
    monkeypatch.setattr(vvhtml, "MAX_DRAW_CELLS", 3)
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "rca" / "rca.f"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    import re
    data = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))
    mods = data["views"]["opt"]["modules"]
    assert "vv-placeholder" in mods["rca"]["hier"]["svg"]                   # 8 instances + glue: above 3 cells
    assert mods["rca"]["hier"]["net"]["cells"]                              # still navigable and simulable
    # a schematic netlistsvg cannot draw: a placeholder and a warning, the other views are fine
    real = vvhtml.run_netlistsvg

    def flaky(json_path, svg_path, *a, **k):
        if "ha" in json_path.read_text()[:400] and '"ha"' in json_path.read_text():
            raise vvhtml.VeriViewError("netlistsvg failed for x\nRangeError: Maximum call stack size exceeded")
        return real(json_path, svg_path, *a, **k)
    monkeypatch.setattr(vvhtml, "MAX_DRAW_CELLS", 3000)
    monkeypatch.setattr(vvhtml, "run_netlistsvg", flaky)
    assert main(["-q", str(EX / "rca" / "rca.f"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    mods = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))["views"]["opt"]["modules"]
    assert "could not be drawn" in mods["ha"]["hier"]["svg"] and "vv-placeholder" not in mods["rca"]["hier"]["svg"]


@needs_tools
def test_gate_view_skipped_above_the_limit(tmp_path):
    import re

    from veriview.cli import main
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "rca" / "rca.f"), "--html", "--gate-limit", "5", "-o", str(out)]) == 0
    data = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))
    assert data["stages"] == ["elab", "opt"] and "gate view skipped" in data["skipped"]["gate"]
    # asked for explicitly: built anyway
    rca = str(EX / "rca" / "rca.f")
    assert main(["-q", rca, "--html", "--stage", "gate", "--gate-limit", "5", "-o", str(out)]) == 0
    data = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))
    assert "gate" in data["stages"] and not data["skipped"]


@needs_tools
def test_big_viewers_store_each_schematic_compressed(tmp_path, monkeypatch):
    import base64
    import gzip
    import re

    from veriview.cli import main
    monkeypatch.setattr(vvhtml, "GZIP_MIN", 1000)
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "rca" / "rca.f"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    packed = json.loads(re.search(r"const DATA = (.*?);\n", out.read_text()).group(1))
    data = json.loads(gzip.decompress(base64.b64decode(packed["gz"])))
    top = data["views"]["opt"]["modules"]["rca"]["hier"]
    assert "svg" not in top and gzip.decompress(base64.b64decode(top["svgz"])).decode().lstrip().startswith("<svg")
    assert "svg" in data["views"]["opt"]["modules"]["ha"]["hier"]          # small ones stay as they are


@needs_tools
@pytest.mark.skipif(not NODE, reason="needs node")
def test_viewer_page_scripts_parse_as_embedded(tmp_path):
    """sim.js and timing.js share one <script> in the viewer: top-level names must not clash."""
    import re

    from veriview.cli import main
    out = tmp_path / "v.html"
    assert main(["-q", str(EX / "counter.sv"), "--html", "--html-stages", "opt", "-o", str(out)]) == 0
    scripts = re.findall(r"<script>(.*?)</script>", out.read_text(), re.S)
    assert scripts
    # all inline scripts run in one global scope: check them together, in page order
    js = tmp_path / "all.js"
    js.write_text("\n;\n".join(scripts))
    r = subprocess.run([NODE, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
