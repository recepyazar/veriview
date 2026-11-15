"""Tests for the launcher (local web UI): API logic and the HTTP server's safety checks."""

import json
import shutil
import threading
import urllib.error
import urllib.request

import pytest
from conftest import EX, needs_tools

from veriview.launcher import Launcher, serve
from veriview.log import VeriViewError


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(EX, root)
    return root


def test_listing_and_path_safety(proj):
    la = Launcher(proj)
    r = la.listing("")
    assert {d["name"] for d in r["dirs"]} >= {"fsm", "rca", "gray"}
    assert "counter.sv" in {f["name"] for f in r["files"]}
    fsm = la.listing("fsm")
    kinds = {f["name"]: f["kind"] for f in fsm["files"]}
    assert kinds["fsm.f"] == "list" and kinds["traffic.sv"] == "source"
    for bad in ("..", "../..", "/etc"):
        with pytest.raises(VeriViewError, match="outside"):
            la.listing(bad)


@needs_tools
def test_candidates_build_and_report(proj):
    la = Launcher(proj)
    assert la.candidates({"files": ["rca/rca.f"]})["candidates"] == ["rca"]
    assert la.candidates({"files": ["fsm/fsm.f"]})["candidates"] == ["multi_fsm"]
    b = la.build({"files": ["rca/rca.f"], "top": "fa", "stages": ["opt"], "lang": "tr"})
    assert b["error"] == "" and b["version"] == 1 and b["name"] == "fa"
    html = la.builds[b["id"]].html.read_text()
    assert '"lang":"tr"' in html and '"top":"fa"' in html
    rep = la.report({"files": ["fsm/fsm.f"], "kind": "fsm"})
    assert "FSM traffic.state" in rep["text"]
    rep = la.report({"files": ["counter.sv"], "kind": "timing", "period": "2ns"})
    assert "Timing report: counter" in rep["text"]
    bad = la.build({"files": ["counter.sv"], "top": "nosuch", "stages": ["opt"]})
    assert "not found" in bad["error"] and bad["version"] == 0
    la.shutdown()


def _req(url, data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_server_security(proj):
    server, la, url = serve(proj, 18800)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _req(url)
        assert code == 200 and "VeriView" in body
        assert _req(url + "api/list", {"dir": ""})[0] == 403                         # no X-VeriView header
        code, body = _req(url + "api/list", {"dir": ""}, {"X-VeriView": "1"})
        assert code == 200 and "counter.sv" in body
        assert _req(url + "api/list", {"dir": ".."}, {"X-VeriView": "1"})[0] == 400  # outside the folder
        assert _req(url, headers={"Host": "evil.example"})[0] == 403                 # DNS rebinding
    finally:
        server.shutdown()
        server.server_close()
        la.shutdown()


def test_editor_save(proj):
    la = Launcher(proj)
    src = "module ha(input a, b, output s); assign s = a ^ b; endmodule\n"
    r = la.save({"path": "drawn/half_adder.v", "text": src})
    assert r == {"path": "drawn/half_adder.v"} and (proj / "drawn" / "half_adder.v").is_file()
    assert la.save({"path": "drawn/half_adder.v", "text": "x"}) == {"exists": True, "path": "drawn/half_adder.v"}
    assert la.save({"path": "drawn/half_adder.v", "text": "y", "overwrite": True})["path"] == "drawn/half_adder.v"
    assert (proj / "drawn" / "half_adder.v").read_text() == "y"
    with pytest.raises(VeriViewError, match="Only .v"):
        la.save({"path": "evil.py", "text": "import os"})
    with pytest.raises(VeriViewError, match="outside"):
        la.save({"path": "../outside.v", "text": "x"})


def test_editor_page_served(proj):
    from veriview.launcher import editor_page
    html = editor_page().decode()
    assert "function toVerilog" in html and '"launcher": true' in html and "createSim" in html
    assert html.count("</script>") == 2


def _wait_attempt(la, bid, attempt, timeout=60):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = la.status(bid)
        if st["attempt"] > attempt:
            return st
        time.sleep(0.1)
    raise AssertionError("no rebuild")


@needs_tools
def test_edit_source_from_viewer(proj):
    la = Launcher(proj)
    b = la.build({"files": ["counter.sv"], "stages": ["opt"]})
    assert b["error"] == "" and la.status(b["id"])["attempt"] == 1
    html = la.builds[b["id"]].html.read_text()
    src = (proj / "counter.sv").read_text()
    r = la.save_source({"id": b["id"], "file": str(proj / "counter.sv"), "text": src.replace("count + 1", "count + 2")})
    assert r == {"path": "counter.sv", "attempt": 1}
    st = _wait_attempt(la, b["id"], 1)
    assert st["error"] == "" and st["version"] == 2
    assert "count + 2" in (proj / "counter.sv").read_text() and la.builds[b["id"]].html.read_text() != html
    # a syntax error: the build fails, the last good viewer stays
    la.save_source({"id": b["id"], "file": "counter.sv", "text": src.replace("endmodule", "")})
    st = _wait_attempt(la, b["id"], 2)
    assert st["error"] and st["version"] == 2
    # only the design's own sources can be written
    (proj / "other.v").write_text("module other; endmodule\n")
    for bad in ("other.v", "../x.v", "/etc/passwd"):
        with pytest.raises(VeriViewError, match="not a source file"):
            la.save_source({"id": b["id"], "file": bad, "text": "x"})
    la.shutdown()


@needs_tools
def test_open_module_in_editor(proj):
    la = Launcher(proj)
    b = la.build({"files": ["rca/rca.f"], "stages": ["opt"]})
    d = la.to_editor({"id": b["id"], "module": "fa"})
    assert d["module"] == "fa" and "error" not in d
    types = {n["type"] for n in d["nodes"]}
    assert {"in", "out"} <= types and types & {"xor", "xnor"}
    whole = la.to_editor({"id": b["id"], "module": ""})          # the build's top, flattened
    assert whole["module"] == "rca" and whole["warn"] == []
    ins = {n["name"]: n.get("bits", 1) for n in whole["nodes"] if n["type"] == "in"}
    assert ins == {"a": 8, "b": 8, "cin": 1}                          # buses stay buses (with a splitter)
    assert sum(n["type"] == "split" for n in whole["nodes"]) == 2 and any(n["type"] == "merge" for n in whole["nodes"])
    assert "error" in la.to_editor({"id": b["id"], "module": "nosuch"})
    la.shutdown()


def test_viewer_gets_server_info(proj):
    server, la, url = serve(proj, 18820)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        from veriview.launcher import Build
        b = Build("abc", [], live=False)
        b.html.write_text("<html><head></head><body>v</body></html>")
        la.builds["abc"] = b
        code, body = _req(url + "view/abc")
        assert code == 200 and 'window.VERIVIEW_SERVER = {"build": "abc"' in body
        code, body = _req(url + "api/source", {"id": "abc", "file": "x.v", "text": ""}, {"X-VeriView": "1"})
        assert code == 400 and "not a source file" in body
    finally:
        server.shutdown()
        server.server_close()
        la.shutdown()


def test_drawings_save_load_and_listing(proj):
    la = Launcher(proj)
    design = {"module": "ha", "nodes": [{"id": 1, "type": "in", "name": "a", "x": 0, "y": 0}], "wires": [], "seq": 2}
    r = la.save_drawing({"path": "drawn/ha", "text": "module ha(input a); endmodule\n", "design": design})
    assert r == {"path": "drawn/ha.v", "drawing": "drawn/ha.veriview.json"}
    assert la.save_drawing({"path": "drawn/ha.v", "text": "x", "design": design})["exists"]
    back = la.load_drawing({"path": "drawn/ha.veriview.json"})
    assert back["design"]["nodes"] == design["nodes"] and back["verilog"] == "drawn/ha.v"
    files = {f["name"]: f for f in la.listing("drawn")["files"]}
    assert files["ha.v"]["drawing"] == "drawn/ha.veriview.json" and "ha.veriview.json" not in files
    (proj / "drawn" / "only.veriview.json").write_text(json.dumps({"veriview": "gate-editor", "nodes": []}))
    assert {f["name"]: f["kind"] for f in la.listing("drawn")["files"]}["only.veriview.json"] == "drawing"
    with pytest.raises(VeriViewError, match="outside"):
        la.load_drawing({"path": "../x.veriview.json"})
    with pytest.raises(VeriViewError, match="Not a drawing"):
        la.load_drawing({"path": "counter.sv"})


@needs_tools
def _view(url, path, timeout=120):
    """GET a /view URL; while it builds the page shows the progress: wait and fetch it again."""
    import time
    end = time.monotonic() + timeout
    while True:
        code, body = _req(url + path)
        if "Building the design" not in body or time.monotonic() > end:
            return code, body
        time.sleep(0.3)


def test_view_urls(proj):
    server, la, url = serve(proj, 18840)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _req(url + "view?f=counter.sv")        # built in the background: a progress page first
        assert code == 200 and "Building the design" in body and "/api/status/" in body
        code, body = _view(url, "view?f=counter.sv")
        assert code == 200 and '"title": "counter.sv"' in body and '"files": ["counter.sv"]' in body
        assert len(la.builds) == 1
        assert _view(url, "view?f=counter.sv")[0] == 200 and len(la.builds) == 1      # the same build again
        # changed on disk since: rebuilt before it is shown (no stale page after saving in the editor)
        b = next(iter(la.builds.values()))
        src = (proj / "counter.sv").read_text()
        (proj / "counter.sv").write_text(src.replace("count + 1", "count + 2"))
        _view(url, "view?f=counter.sv")
        assert b.version >= 2
        (proj / "bad.v").write_text("module bad(input a; endmodule\n")
        code, body = _view(url, "view?f=bad.v")
        assert code == 200 and "could not be built" in body and "syntax error" in body
        assert 'name="top"' in body                                                   # another top can be chosen
        assert _req(url + "view?f=../x.v")[0] == 400
        # a drawn design: the viewer knows its drawing (Edit opens the gate editor)
        la.save_drawing({"path": "ha.v", "text": "module ha(input a, b, output s); assign s = a ^ b; endmodule\n",
                         "design": {"nodes": [], "wires": []}})
        code, body = _view(url, "view?f=ha.v")
        assert code == 200 and '"drawing": "ha.veriview.json"' in body
        d = la.to_editor({"files": ["rca/rca.f"], "module": "fa"})
        assert d["module"] == "fa" and "error" not in d
    finally:
        server.shutdown()
        server.server_close()
        la.shutdown()


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_generated_pages_have_valid_scripts(proj, tmp_path):
    """Every inline script of the pages the launcher writes parses (Python strings hide JS escapes)."""
    import re
    import subprocess

    from veriview.launcher import Build, building_page, editor_page, error_page, page
    from veriview.watch import LIVE_RELOAD_JS
    la = Launcher(proj)
    pages = {"start": page(la).decode(), "editor": editor_page("/x").decode(),
             "building": building_page({"title": "t"}, Build("b1", [], live=True)).decode(),
             "error": error_page("Synthesis failed.\nTry a synthesizable top module: a, b",
                                 {"title": "t", "files": ["x.sv"]}, LIVE_RELOAD_JS).decode()}
    for name, html in pages.items():
        for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
            f = tmp_path / f"{name}{i}.js"
            f.write_text(js.replace("/*__EDITOR_DATA__*/null", "null"))
            r = subprocess.run([shutil.which("node"), "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, f"{name} script {i}: {r.stderr}"


@needs_tools
def test_big_schematics_drawn_on_demand(proj, monkeypatch):
    import base64
    import gzip
    import re

    from veriview import html as vvhtml
    monkeypatch.setattr(vvhtml, "LAZY_MIN_CELLS", 2)        # rca counts as big: only the tops are drawn first
    server, la, url = serve(proj, 18860)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def data_of(page):
        d = json.loads(re.search(r"const DATA = (.*?);\n", page).group(1))
        return json.loads(gzip.decompress(base64.b64decode(d["gz"]))) if "gz" in d else d
    try:
        code, body = _view(url, "view?f=rca/rca.f")
        assert code == 200
        bid = re.search(r'"build": "(\w+)"', body).group(1)
        mods = data_of(body)["views"]["opt"]["modules"]
        lazy = {n: m["hier"]["lazy"] for n, m in mods.items() if "lazy" in m["hier"]}
        assert lazy and "rca" not in lazy and "svg" in mods["rca"]["hier"]      # the top is drawn right away
        name, idx = next(iter(lazy.items()))
        code, body = _req(url + f"api/view/{bid}/{idx}", timeout=180)     # drawing takes a moment
        assert code == 200 and json.loads(body)["svg"].lstrip().startswith("<svg")
        # a flattened view: flattened on demand, its netlist comes along
        fidx = mods["rca"]["flat"]["lazy"]
        r = json.loads(_req(url + f"api/view/{bid}/{fidx}", timeout=180)[1])
        assert r["svg"].lstrip().startswith("<svg") and len(r["net"]["cells"]) > 8      # no fa instances left
        assert _req(url + f"api/view/{bid}/99999")[0] == 404 and _req(url + "api/view/nosuch/1")[0] == 404
        # the downloadable copy has every schematic in it
        code, full = _req(url + f"view/{bid}?download=1", timeout=600)
        mods = data_of(full)["views"]["opt"]["modules"]
        assert code == 200 and all("lazy" not in m["hier"] for m in mods.values())
        assert mods["rca"]["flat"]["net"] and "lazy" not in mods["rca"]["flat"]
        assert all("svg" in m["hier"] or "svgz" in m["hier"] for m in mods.values())
    finally:
        server.shutdown()
        server.server_close()
        la.shutdown()
