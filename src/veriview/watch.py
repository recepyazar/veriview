"""--watch: rebuild on source changes; for --html, serve the viewer with live reload.

Polling (stdlib only): the watched set is every source file, every .f file list
and every HDL file inside the include directories of the last build. With
--html a small HTTP server on 127.0.0.1 serves the viewer with an injected
script that polls /__veriview/status and reloads the page after a successful
rebuild (the viewer keeps its position in the URL hash, so the reload lands on
the same instance/view). Failed rebuilds show an error banner in the page and
keep the last good version.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import log
from .filelist import SourceSet
from .log import VeriViewError
from .render import open_path, open_url

BuildResult = tuple[int, "Path | None", SourceSet]

HDL_GLOBS = ("*.v", "*.sv", "*.vh", "*.svh")
STATUS_PATH = "/__veriview/status"

LIVE_RELOAD_JS = """
<script>
(() => {
  let version = null;
  const bar = document.createElement("div");
  bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:1000;padding:10px 14px;background:#b42318;" +
    "color:#fff;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;" +
    "max-height:40vh;overflow:auto;display:none";
  const attach = () => document.body.append(bar);
  if (document.body) attach(); else document.addEventListener("DOMContentLoaded", attach);
  const poll = async () => {
    try {
      const s = await (await fetch("__STATUS_PATH__", { cache: "no-store" })).json();
      if (version === null) version = s.version;
      else if (s.version !== version) { location.reload(); return; }
      bar.style.display = s.error ? "block" : "none";
      bar.textContent = s.error ? "VeriView: rebuild failed (showing the last good version)\\n\\n" + s.error : "";
    } catch (e) { /* server stopped */ }
    setTimeout(poll, 1000);
  };
  poll();
})();
</script>
""".replace("__STATUS_PATH__", STATUS_PATH)

WAITING_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>VeriView</title></head>
<body style="font:15px system-ui,sans-serif;padding:24px">
<p>VeriView: waiting for the first successful build…</p></body></html>"""


def watched_files(sources: SourceSet | None, inputs: list[str]) -> set[Path]:
    files = {Path(i) for i in inputs}
    if sources is not None:
        files.update(sources.files)
        files.update(sources.filelists)
        for d in sources.incdirs:
            if d.is_dir():
                for pattern in HDL_GLOBS:
                    files.update(d.glob(pattern))
    return files


def snapshot(files: set[Path]) -> dict[Path, float | None]:
    out: dict[Path, float | None] = {}
    for f in files:
        try:
            out[f] = f.stat().st_mtime
        except OSError:
            out[f] = None
    return out


class LiveState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.version = 0
        self.error = ""
        self.output: Path | None = None

    def status(self) -> dict:
        with self.lock:
            return {"version": self.version, "error": self.error}


def make_server(state: LiveState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0]
            if path == STATUS_PATH:
                self._send(200, "application/json", json.dumps(state.status()).encode())
            elif path in ("/", "/index.html"):
                with state.lock:
                    out = state.output
                page = out.read_text(encoding="utf-8") if out and out.is_file() else WAITING_PAGE
                if "</body>" in page:
                    page = page.replace("</body>", LIVE_RELOAD_JS + "</body>", 1)
                else:
                    page += LIVE_RELOAD_JS
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:  # keep the terminal quiet
            pass

    last_err: OSError | None = None
    for p in range(port, port + 20) if port else [0]:
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError as e:  # port in use
            last_err = e
    raise VeriViewError(f"Could not start the live server on ports {port}-{port + 19}: {last_err}")


def watch(build: Callable[[], BuildResult], *, args: argparse.Namespace, inputs: list[str],
          interval: float = 0.5, stop: threading.Event | None = None,
          on_ready: Callable[[str | None], None] | None = None) -> int:
    """Build, then rebuild whenever a watched file changes, until Ctrl+C (or `stop` is set)."""
    stop = stop or threading.Event()
    state = LiveState()
    server = None
    url = None
    if args.html:
        server = make_server(state, args.port)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        threading.Thread(target=server.serve_forever, daemon=True).start()

    sources: SourceSet | None = None

    def rebuild(first: bool) -> None:
        nonlocal sources
        started = time.monotonic()
        try:
            status, out, sources = build()
        except VeriViewError as e:
            log.error(str(e))
            with state.lock:
                state.error = str(e)
            return
        with state.lock:
            state.output = out
            state.error = "" if status == 0 else "Some modules failed; see the terminal."
            state.version += 1
        log.info(f"{'Built' if first else 'Rebuilt'} in {time.monotonic() - started:.1f}s")
        if first and args.open and out is not None and not url:
            open_path(out)

    try:
        rebuild(first=True)
        if url:
            log.info(f"Live viewer: {url}  (reloads on every change)")
            if args.open:
                open_url(url)
        if on_ready:
            on_ready(url)
        log.info("Watching for changes... (Ctrl+C to stop)")
        files = watched_files(sources, inputs)
        snap = snapshot(files)
        while not stop.wait(interval):
            new = snapshot(files)
            if new == snap:
                continue
            changed = sorted(str(f) for f in files if new.get(f) != snap.get(f))
            # let editors finish writing (save = truncate + write, or several files at once)
            stop.wait(0.2)
            log.info(f"Changed: {', '.join(changed[:3])}{' ...' if len(changed) > 3 else ''}")
            rebuild(first=False)
            files = watched_files(sources, inputs)
            snap = snapshot(files)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        if server:
            server.shutdown()
            server.server_close()
    return 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
