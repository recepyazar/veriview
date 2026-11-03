"""Tests for --watch (file polling, rebuilds and the live-reload server)."""

import argparse
import json
import threading
import time
import urllib.request
from pathlib import Path

from veriview.filelist import SourceSet
from veriview.log import VeriViewError
from veriview.watch import STATUS_PATH, free_port, snapshot, watch, watched_files


def test_watched_files(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "defs.svh").write_text("")
    (inc / "notes.txt").write_text("")
    src = SourceSet(files=[tmp_path / "a.sv"], incdirs=[inc], filelists=[tmp_path / "x.f"])
    files = watched_files(src, ["top.f"])
    assert files == {Path("top.f"), tmp_path / "a.sv", tmp_path / "x.f", inc / "defs.svh"}


def test_snapshot_detects_changes(tmp_path):
    f = tmp_path / "a.sv"
    f.write_text("1")
    s1 = snapshot({f, tmp_path / "missing.sv"})
    assert s1[tmp_path / "missing.sv"] is None
    time.sleep(0.01)
    f.write_text("2")
    import os
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert snapshot({f}) != {f: s1[f]}


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_watch_live_server(tmp_path):
    """Rebuilds on change, serves the page with live reload, reports errors."""
    src = tmp_path / "top.sv"
    src.write_text("ok")
    out = tmp_path / "view.html"
    builds = []

    def build():
        text = src.read_text()
        if "error" in text:
            raise VeriViewError("syntax error in top.sv")
        builds.append(text)
        out.write_text(f"<html><body>{text}</body></html>")
        return 0, out, SourceSet(files=[src])

    args = argparse.Namespace(html=True, port=free_port(), open=False)
    stop = threading.Event()
    ready = {}
    t = threading.Thread(target=watch, args=(build,),
                         kwargs=dict(args=args, inputs=[str(src)], interval=0.05, stop=stop,
                                     on_ready=lambda url: ready.setdefault("url", url)), daemon=True)
    t.start()
    try:
        assert _wait(lambda: "url" in ready)
        url = ready["url"]
        status = lambda: json.loads(_get(url + STATUS_PATH[1:]))  # noqa: E731
        assert status() == {"version": 1, "error": ""}
        page = _get(url)
        assert "ok" in page and STATUS_PATH in page  # live-reload script injected

        def touch(text):
            src.write_text(text)
            st = src.stat()
            import os
            os.utime(src, (st.st_atime, st.st_mtime + len(builds) + 10))

        touch("ok2")
        assert _wait(lambda: status()["version"] == 2)
        assert "ok2" in _get(url)

        touch("error")
        assert _wait(lambda: status()["error"] == "syntax error in top.sv")
        assert status()["version"] == 2  # last good version kept

        touch("ok3")
        assert _wait(lambda: status() == {"version": 3, "error": ""})
    finally:
        stop.set()
        t.join(timeout=5)
    assert builds == ["ok", "ok2", "ok3"]
