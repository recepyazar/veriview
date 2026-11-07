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
from urllib.parse import parse_qs

from . import __version__, log
from .log import VeriViewError
from .render import open_url
from .watch import LIVE_RELOAD_JS, snapshot, watched_files

TEMPLATE = Path(__file__).with_name("launcher.html")
EDITOR = Path(__file__).with_name("editor.html")
HDL_SUFFIXES = (".v", ".sv", ".svh", ".vh", ".f")
DRAWING = ".veriview.json"   # a gate-editor drawing, saved next to the Verilog generated from it


def drawing_of(path: Path) -> Path:
    """counter.v -> counter.veriview.json (the drawing the file was generated from, if any)."""
    return path.with_name(path.name.rsplit(".", 1)[0] + DRAWING)
HIDDEN_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


class Build:
    def __init__(self, bid: str, argv: list[str], live: bool):
        self.id, self.argv, self.live = bid, argv, live
        self.dir = Path(tempfile.mkdtemp(prefix="veriview-ui."))
        self.html = self.dir / "viewer.html"
        self.version = 0
        self.attempt = 0          # rebuilds tried (successful or not)
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
        self.by_key: dict[str, str] = {}   # /view?f=... -> build id (one build per file set and options)
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
                    dirs.append({"path": r, "name": e.name, "count": n,
                                 "lists": sorted(x.relative_to(self.root).as_posix() for x in e.glob("*.f"))})
            elif e.suffix in HDL_SUFFIXES:
                kind = "list" if e.suffix == ".f" else ("header" if e.suffix in (".svh", ".vh") else "source")
                f = {"path": r, "name": e.name, "kind": kind, "size": e.stat().st_size}
                if kind == "source" and drawing_of(e).is_file():
                    f["drawing"] = drawing_of(e).relative_to(self.root).as_posix()
                files.append(f)
            elif e.name.endswith(DRAWING):
                v = [e.with_name(e.name[:-len(DRAWING)] + x) for x in (".v", ".sv")]
                if not any(x.is_file() for x in v):   # a drawing without its Verilog (yet)
                    files.append({"path": r, "name": e.name, "kind": "drawing", "size": e.stat().st_size})
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

    @staticmethod
    def view_key(req: dict[str, Any]) -> str:
        return json.dumps([req.get(k) or None for k in ("files", "top", "flatten", "incdirs", "defines", "frontend")])

    def open_view(self, req: dict[str, Any]) -> Build:
        """The (live) build for a file set and options: built on first use, then reused (/view?f=...)."""
        key = self.view_key(req)
        bid = self.by_key.get(key)
        if bid and bid in self.builds:
            b = self.builds[bid]
            # changed since the last build (e.g. saved by the editor a moment ago): rebuild before showing it
            if snapshot(watched_files(b.sources, b.inputs)) != getattr(b, "snap", None):
                self._rebuild(b)
            return b
        r = self.build({**req, "live": True})
        self.by_key[key] = r["id"]
        return self.builds[r["id"]]

    def view_info(self, b: Build) -> dict[str, Any]:
        """What the viewer page needs to know about its server (editing, the drawing, going home)."""
        info: dict[str, Any] = {"build": b.id, "editor": "/editor", "home": "/", "live": b.live, "root": str(self.root),
                                "files": list(getattr(b, "req", {}).get("files", []))}
        files = [self.safe(f) for f in info["files"]]
        if len(files) == 1 and files[0].suffix in (".v", ".sv") and drawing_of(files[0]).is_file():
            info["drawing"] = drawing_of(files[0]).relative_to(self.root).as_posix()
        top = getattr(b, "req", {}).get("top")
        info["title"] = top or (Path(info["files"][0]).name if len(info["files"]) == 1 else b.name)
        return info

    def load_drawing(self, req: dict[str, Any]) -> dict[str, Any]:
        rel = str(req.get("path", ""))
        if not rel.endswith(DRAWING):
            raise VeriViewError("Not a drawing")
        p = self.safe(rel)
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("veriview") != "gate-editor" or not isinstance(d.get("nodes"), list):
            raise VeriViewError(f"{rel} is not a VeriView drawing")
        stem = p.name[:-len(DRAWING)]
        verilog = next((x for x in (p.with_name(stem + e) for e in (".v", ".sv")) if x.is_file()), None)
        return {"design": d, "path": rel,
                "verilog": verilog.relative_to(self.root).as_posix() if verilog else None}

    def save_drawing(self, req: dict[str, Any]) -> dict[str, Any]:
        """Save a drawing: <stem>.v (the generated Verilog) + <stem>.veriview.json (the drawing)."""
        rel = str(req.get("path", "")).strip()
        if rel.endswith(DRAWING):
            rel = rel[:-len(DRAWING)] + ".v"
        if not rel.endswith((".v", ".sv")):
            rel += ".v"
        r = self.save({"path": rel, "text": req.get("text", ""), "overwrite": req.get("overwrite")})
        if r.get("exists"):
            return r
        v = self.safe(r["path"])
        design = req.get("design") or {}
        drawing_of(v).write_text(json.dumps({"veriview": "gate-editor", "version": 1, **design}, indent=1),
                                 encoding="utf-8")
        return {"path": r["path"], "drawing": drawing_of(v).relative_to(self.root).as_posix()}

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
        b.snap = snapshot(watched_files(b.sources, b.inputs))
        b.attempt += 1

    def _watch(self, b: Build) -> None:
        # compares with the state of the last build (b.snap), whoever made it (this loop, a save, opening it)
        while not b.stop.wait(0.5):
            if snapshot(watched_files(b.sources, b.inputs)) == getattr(b, "snap", None):
                continue
            b.stop.wait(0.2)
            self._rebuild(b)

    def status(self, bid: str) -> dict[str, Any]:
        b = self.builds.get(bid)
        if not b:
            raise VeriViewError("Unknown build")
        return {"version": b.version, "attempt": b.attempt, "error": b.error, "log": b.log, "live": b.live,
                "name": b.name}

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

    def save(self, req: dict[str, Any]) -> dict[str, Any]:
        """Write a Verilog file from the gate editor (only .v/.sv, below the start folder)."""
        rel = str(req.get("path", "")).strip()
        if not rel.endswith((".v", ".sv")):
            raise VeriViewError("Only .v / .sv files can be saved.")
        p = self.safe(rel)
        if p.exists() and not req.get("overwrite"):
            return {"exists": True, "path": p.relative_to(self.root).as_posix()}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(req.get("text", "")), encoding="utf-8")
        log.info(f"Gate editor saved {p}")
        return {"path": p.relative_to(self.root).as_posix()}

    def save_source(self, req: dict[str, Any]) -> dict[str, Any]:
        """Write a source file edited in the viewer's code panel, then rebuild that viewer.

        Only files that are part of the build (and below the start folder) can be written.
        A live build notices the change by itself; others are rebuilt in the background.
        The page polls /api/status until `attempt` grows.
        """
        b = self.builds.get(str(req.get("id")))
        if not b:
            raise VeriViewError("Unknown build")
        name = str(req.get("file", ""))
        files = list(b.sources.files) if b.sources is not None else []
        hits = [f for f in files if f.as_posix() == name or f.resolve() == Path(name).resolve()
                or f.as_posix().endswith("/" + name.lstrip("./"))]
        if len(hits) != 1:
            raise VeriViewError(f"{name} is not a source file of this design (generated files cannot be edited)")
        p = self.safe(str(hits[0].resolve()))
        attempt = b.attempt
        p.write_text(str(req.get("text", "")), encoding="utf-8")
        log.info(f"Viewer saved {p}")
        if not b.live:
            threading.Thread(target=self._rebuild, args=(b,), daemon=True).start()
        return {"path": p.relative_to(self.root).as_posix(), "attempt": attempt}

    def to_editor(self, req: dict[str, Any]) -> dict[str, Any]:
        """A module of a build as a gate-editor drawing (flattened, gate level)."""
        from .cli import prepare
        from .filelist import collect_sources
        from .synth import Synthesizer
        from .to_editor import netlist_to_design
        if req.get("files"):   # by file set (an /editor?from=... URL survives server restarts)
            argv = self.argv_for({**req, "top": None}, "--html")
        else:
            b = self.builds.get(str(req.get("id")))
            if not b:
                raise VeriViewError("Unknown build")
            argv = b.argv
        module = str(req.get("module") or req.get("top") or "")
        if module.startswith("$paramod"):   # parameterized instance: the module with its default parameters
            module = module.split("\\")[1] if "\\" in module else module
        module = module.lstrip("\\") or None
        lines: list[str] = []

        def go() -> dict[str, Any]:
            args, inputs, _ = prepare(argv)
            sources = collect_sources(inputs, args.incdirs, args.defines)
            with tempfile.TemporaryDirectory(prefix="veriview.") as tmp:
                out = Path(tmp) / "editor.json"
                Synthesizer(sources, Path(tmp), frontend=args.frontend).synthesize(module or args.top, out,
                                                                                   stage="editor")
                return netlist_to_design(json.loads(out.read_text()))
        try:
            return self.run_captured(go, lines)
        except VeriViewError as e:
            return {"error": str(e), "log": lines}

    def shutdown(self) -> None:
        for b in self.builds.values():
            b.stop.set()
            shutil.rmtree(b.dir, ignore_errors=True)


def page(launcher: Launcher) -> bytes:
    data = json.dumps({"version": __version__, "root": str(launcher.root)}).replace("<", "\\u003c")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__LAUNCHER_DATA__*/null", data).encode("utf-8")


def editor_page(root: str = "") -> bytes:
    data = json.dumps({"version": __version__, "launcher": True, "root": root}).replace("<", "\\u003c")
    core = Path(__file__).with_name("editor_core.js").read_text(encoding="utf-8").replace("</", "<\\/")
    return (EDITOR.read_text(encoding="utf-8").replace("/*__EDITOR_CORE__*/", core, 1)
            .replace("/*__EDITOR_DATA__*/null", data).encode("utf-8"))


ERROR_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>VeriView</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1d232b}
@media (prefers-color-scheme: dark){body{background:#111418;color:#e6e9ed}pre{background:#181c21!important}}
header{padding:10px 16px;border-bottom:1px solid #8884}a{color:#2563eb}
main{padding:16px;max-width:900px}pre{background:#fff;border:1px solid #8884;border-radius:8px;padding:10px;
white-space:pre-wrap;font:12.5px/1.5 ui-monospace,Menlo,monospace}</style></head>
<body><header><a href="/"><b>VeriView</b></a> &nbsp;·&nbsp; __TITLE__</header><main>
<h3 data-tr="Tasarım oluşturulamadı.">The design could not be built.</h3><pre>__ERROR__</pre>
<p data-tr="__HINT_TR__">__HINT__</p></main>
<script>try{if(((localStorage.getItem("veriview-lang")||navigator.language)+"").toLowerCase().startsWith("tr"))
for(const e of document.querySelectorAll("[data-tr]"))e.innerHTML=e.dataset.tr}catch(e){}</script></body></html>"""


def error_page(message: str, info: dict[str, Any] | None, js: str = "") -> bytes:
    import html as _h
    if info:
        hint = "Fix the file (in any text editor) and save: this page reloads by itself once the design builds."
        hint_tr = ("Dosyayı (herhangi bir metin editöründe) düzeltip kaydet: "
                   "tasarım oluşunca bu sayfa kendiliğinden yenilenir.")
    else:
        hint, hint_tr = '<a href="/">Back to the start page</a>', '<a href="/">Başlangıç sayfasına dön</a>'
    page = (ERROR_PAGE.replace("__TITLE__", _h.escape(str((info or {}).get("title", ""))))
            .replace("__ERROR__", _h.escape(message or "unknown error"))
            .replace("__HINT_TR__", _h.escape(hint_tr)).replace("__HINT__", hint))
    return (page.replace("</body>", js + "</body>", 1) if js else page).encode("utf-8")


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
            if path == "/editor":
                return self._send(200, "text/html; charset=utf-8", editor_page(str(launcher.root)))
            if path == "/view":
                q = parse_qs(query)
                req = {"files": q.get("f", []), "top": (q.get("top") or [None])[0], "flatten": q.get("flat") == ["1"],
                       "incdirs": q.get("I", []), "defines": q.get("D", []), "frontend": (q.get("fe") or [None])[0]}
                try:
                    b = launcher.open_view(req)
                except VeriViewError as e:
                    return self._send(400, "text/html; charset=utf-8", error_page(str(e), None))
                if not b.html.is_file():   # the first build failed: show why, reload once it builds
                    js = LIVE_RELOAD_JS.replace("/__veriview/status", f"/api/status/{b.id}")
                    return self._send(200, "text/html; charset=utf-8", error_page(b.error, launcher.view_info(b), js))
                return self._viewer(b, query)
            if path.startswith("/view/"):
                b = launcher.builds.get(path[len("/view/"):])
                if not b or not b.html.is_file():
                    return self._send(404, "text/plain", b"no such viewer")
                return self._viewer(b, query)
            if path.startswith("/api/status/"):
                try:
                    return self._json(launcher.status(path[len("/api/status/"):]))
                except VeriViewError as e:
                    return self._json({"error": str(e)}, 404)
            self._send(404, "text/plain", b"not found")

        def _viewer(self, b: Build, query: str) -> None:
            html = b.html.read_text(encoding="utf-8")
            if "download=1" in query:
                return self._send(200, "text/html; charset=utf-8", html.encode("utf-8"),
                                  {"Content-Disposition": f'attachment; filename="{b.name}.html"'})
            info = json.dumps(launcher.view_info(b)).replace("<", "\\u003c")
            html = html.replace("<head>", f"<head><script>window.VERIVIEW_SERVER = {info};</script>", 1)
            if b.live:
                js = LIVE_RELOAD_JS.replace("/__veriview/status", f"/api/status/{b.id}")
                html = html.replace("</body>", js + "</body>", 1)
            return self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

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
                if route == "/api/save":
                    return self._json(launcher.save(req))
                if route == "/api/source":
                    return self._json(launcher.save_source(req))
                if route == "/api/to_editor":
                    return self._json(launcher.to_editor(req))
                if route == "/api/drawing/load":
                    return self._json(launcher.load_drawing(req))
                if route == "/api/drawing/save":
                    return self._json(launcher.save_drawing(req))
                return self._json({"error": "unknown API"}, 404)
            except VeriViewError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)
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


def launch(root: Path, port: int = 8765, open_browser: bool = True, start: str = "") -> int:
    if not root.is_dir():
        raise VeriViewError(f"Not a folder: {root}")
    server, launcher, url = serve(root, port)
    log.info(f"VeriView launcher: {url}  (folder: {launcher.root}) — Ctrl+C to stop")
    if open_browser:
        open_url(url + start.lstrip("/"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        server.server_close()
        launcher.shutdown()
    return 0
