"""The launcher: a small local web page to pick files, the top module and options.

`veriview` (no arguments, in a terminal) or `veriview --ui [folder]` starts an HTTP
server on 127.0.0.1 and opens launcher.html. The page lists the HDL files of the
folder, asks Yosys for possible top modules, and builds viewers (optionally live:
rebuilt whenever a source file changes, the viewer tab reloads by itself) or text
reports (timing, FSMs) through exactly the same code path as the command line.

Security: the server only listens on 127.0.0.1, only serves files below the start
folder, checks the Host header (DNS rebinding) and requires a custom request header
on API calls, so other web pages cannot drive it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, log
from .log import VeriViewError
from .render import open_url
from .watch import LIVE_RELOAD_JS, snapshot, watched_files

TEMPLATE = Path(__file__).with_name("launcher.html")
HDL_SUFFIXES = (".v", ".sv", ".svh", ".vh", ".f")
HIDDEN_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


class Build:
    def __init__(self, bid: str, argv: list[str], live: bool):
        self.id, self.argv, self.live = bid, argv, live
        self.dir = Path(tempfile.mkdtemp(prefix="veriview-ui."))
        self.html = self.dir / "viewer.html"
        self.version = 0
        self.error = ""
        self.log: list[str] = []
        self.sources: Any = None
        self.inputs: list[str] = []
        self.name = "viewer"
        self.stop = threading.Event()


class Launcher:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.builds: dict[str, Build] = {}
        self.lock = threading.Lock()   # one Yosys run at a time (logs and stdout are global)

    # --- helpers -------------------------------------------------------------------------
    def safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise VeriViewError("Path outside of the start folder")
        return p

    def listing(self, rel: str) -> dict[str, Any]:
        d = self.safe(rel or ".")
        if not d.is_dir():
            raise VeriViewError(f"Not a folder: {rel}")
        dirs, files = [], []
        for e in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            if e.name.startswith(".") or e.name in HIDDEN_DIRS:
                continue
            r = e.relative_to(self.root).as_posix()
            if e.is_dir():
                n = sum(1 for _ in e.rglob("*") if _.suffix in HDL_SUFFIXES)
                if n:
                    dirs.append({"path": r, "name": e.name, "count": n})
            elif e.suffix in HDL_SUFFIXES:
                files.append({"path": r, "name": e.name, "kind": "list" if e.suffix == ".f" else
                              ("header" if e.suffix in (".svh", ".vh") else "source"), "size": e.stat().st_size})
        rel_d = "" if d == self.root else d.relative_to(self.root).as_posix()
        return {"root": str(self.root), "dir": rel_d, "dirs": dirs, "files": files}

    def argv_for(self, req: dict[str, Any], *extra: str) -> list[str]:
        files = [str(self.safe(f)) for f in req.get("files", [])]
        if not files:
            raise VeriViewError("Select at least one source file or file list.")
        argv = [*files, "-q"]
        if req.get("top"):
            argv += ["-t", str(req["top"])]
        for d in req.get("incdirs", []):
            argv += ["-I", str(self.safe(d))]
        for d in req.get("defines", []):
            argv += ["-D", str(d)]
        if req.get("frontend") and req["frontend"] != "auto":
            argv += ["--frontend", str(req["frontend"])]
        return argv + list(extra)

    def run_captured(self, fn, sink_list: list[str]):
        def sink(tag: str, msg: str) -> None:
            sink_list.append(f"[{tag}] {msg}")
        with self.lock:
            log.add_sink(sink)
            try:
                return fn()
            finally:
                log.remove_sink(sink)

    # --- API ------------------------------------------------------------------------------
    def candidates(self, req: dict[str, Any]) -> dict[str, Any]:
        from .cli import prepare
        from .filelist import collect_sources
        from .synth import Synthesizer
        argv = self.argv_for({**req, "top": None}, "--html")
        lines: list[str] = []

        def go() -> list[str]:
            args, inputs, _ = prepare(argv)
            sources = collect_sources(inputs, args.incdirs, args.defines)
            with tempfile.TemporaryDirectory(prefix="veriview.") as tmp:
                return Synthesizer(sources, Path(tmp), frontend=args.frontend).top_candidates()
        try:
            cands = self.run_captured(go, lines)
        except VeriViewError as e:
            return {"candidates": [], "error": str(e), "log": lines}
        return {"candidates": cands, "log": lines}

    def build(self, req: dict[str, Any], bid: str | None = None) -> dict[str, Any]:
        stages = [s for s in req.get("stages", ["elab", "opt", "gate"]) if s in ("elab", "opt", "gate")] or ["opt"]
        default = req.get("stage") if req.get("stage") in stages else stages[0]
        extra = ["--html-stages", ",".join(stages), "--stage", default, "--lang", str(req.get("lang", "auto")),
                 "--no-open"]
        if req.get("flatten"):
            extra.append("--flatten")
        b = self.builds.get(bid) if bid else None
        if b is None:
            b = Build(uuid.uuid4().hex[:10], self.argv_for(req, *extra), bool(req.get("live")))
            b.req = req
            self.builds[b.id] = b
        self._rebuild(b)
        if b.live and not getattr(b, "watcher", None):
            b.watcher = threading.Thread(target=self._watch, args=(b,), daemon=True)
            b.watcher.start()
        return self.status(b.id) | {"id": b.id, "url": f"/view/{b.id}"}

    def _rebuild(self, b: Build) -> None:
        from .cli import _build, prepare
        lines: list[str] = []

        def go():
            args, inputs, stages = prepare(b.argv + ["-o", str(b.html)])
            b.inputs = inputs
            b.name = (args.top or args.stem_hint or Path(inputs[0]).stem) if inputs else "viewer"
            return _build(args, inputs, stages)
        started = time.monotonic()
        try:
            _status, _out, b.sources = self.run_captured(go, lines)
            b.error = ""
            b.version += 1
            lines.append(f"[INFO] Built in {time.monotonic() - started:.1f}s")
        except VeriViewError as e:
            b.error = str(e)
        except Exception as e:  # never let a watcher thread die on an unexpected error
            b.error = f"{type(e).__name__}: {e}"
        b.log = lines[-200:]

    def _watch(self, b: Build) -> None:
        files = watched_files(b.sources, b.inputs)
        snap = snapshot(files)
        while not b.stop.wait(0.5):
            new = snapshot(files)
            if new == snap:
                continue
            b.stop.wait(0.2)
            self._rebuild(b)
            files = watched_files(b.sources, b.inputs)
            snap = snapshot(files)

    def status(self, bid: str) -> dict[str, Any]:
        b = self.builds.get(bid)
        if not b:
            raise VeriViewError("Unknown build")
        return {"version": b.version, "error": b.error, "log": b.log, "live": b.live, "name": b.name}

    def report(self, req: dict[str, Any]) -> dict[str, Any]:
        from .cli import prepare, report_text
        kind = req.get("kind")
        if kind == "timing":
            extra = ["--timing", str(req.get("period") or "2ns")]
        elif kind == "fsm":
            extra = ["--fsm"]
        else:
            raise VeriViewError("Unknown report")
        lines: list[str] = []
        try:
            text = self.run_captured(lambda: report_text(*prepare(self.argv_for(req, *extra))[:2]), lines)
        except VeriViewError as e:
            return {"text": "", "error": str(e), "log": lines}
        return {"text": text, "log": lines}

    def shutdown(self) -> None:
        for b in self.builds.values():
            b.stop.set()
            shutil.rmtree(b.dir, ignore_errors=True)


def page(launcher: Launcher) -> bytes:
    data = json.dumps({"version": __version__, "root": str(launcher.root)}).replace("<", "\\u003c")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__LAUNCHER_DATA__*/null", data).encode("utf-8")


def make_handler(launcher: Launcher, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, code: int, ctype: str, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, "application/json", json.dumps(obj).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, "text/plain", b"forbidden")
            path, _, query = self.path.partition("?")
            if path in ("/", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", page(launcher))
            if path.startswith("/view/"):
                b = launcher.builds.get(path[len("/view/"):])
                if not b or not b.html.is_file():
                    return self._send(404, "text/plain", b"no such viewer")
                html = b.html.read_text(encoding="utf-8")
                if "download=1" in query:
                    return self._send(200, "text/html; charset=utf-8", html.encode("utf-8"),
                                      {"Content-Disposition": f'attachment; filename="{b.name}.html"'})
                if b.live:
                    js = LIVE_RELOAD_JS.replace("/__veriview/status", f"/api/status/{b.id}")
                    html = html.replace("</body>", js + "</body>", 1)
                return self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
            if path.startswith("/api/status/"):
                try:
                    return self._json(launcher.status(path[len("/api/status/"):]))
                except VeriViewError as e:
                    return self._json({"error": str(e)}, 404)
            self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:  # noqa: N802
            # custom header: forces a CORS preflight, which this server never answers -> no cross-site calls
            if not self._host_ok() or self.headers.get("X-VeriView") != "1":
                return self._send(403, "text/plain", b"forbidden")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                route = self.path.split("?", 1)[0]
                if route == "/api/list":
                    return self._json(launcher.listing(req.get("dir", "")))
                if route == "/api/candidates":
                    return self._json(launcher.candidates(req))
                if route == "/api/build":
                    return self._json(launcher.build(req))
                if route == "/api/rebuild":
                    return self._json(launcher.build({}, bid=req.get("id")))
                if route == "/api/report":
                    return self._json(launcher.report(req))
                return self._json({"error": "unknown API"}, 404)
            except VeriViewError as e:
                return self._json({"error": str(e)}, 400)
            except (ValueError, KeyError) as e:
                return self._json({"error": f"bad request: {e}"}, 400)

        def log_message(self, *a) -> None:
            pass

    return Handler


def serve(root: Path, port: int) -> tuple[ThreadingHTTPServer, Launcher, str]:
    launcher = Launcher(root)
    last: OSError | None = None
    for p in range(port, port + 20):
        hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(launcher, hosts))
            return server, launcher, f"http://127.0.0.1:{p}/"
        except OSError as e:
            last = e
    raise VeriViewError(f"Could not start the launcher on ports {port}-{port + 19}: {last}")


def launch(root: Path, port: int = 8765, open_browser: bool = True) -> int:
    if not root.is_dir():
        raise VeriViewError(f"Not a folder: {root}")
    server, launcher, url = serve(root, port)
    log.info(f"VeriView launcher: {url}  (folder: {launcher.root}) — Ctrl+C to stop")
    if open_browser:
        open_url(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        server.server_close()
        launcher.shutdown()
    return 0
