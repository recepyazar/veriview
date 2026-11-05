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


def _req(url, data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
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
    assert "function toVerilog" in html and '"launcher": true' in html
    assert html.count("</script>") == 2
