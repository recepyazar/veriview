"""netlistsvg rendering, SVG post-processing and opening the result."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import log
from .log import VeriViewError
from .tools import find_tool

_SVG_OPEN_RE = re.compile(r"<svg[^>]*>")


def add_background(svg_text: str, color: str) -> str:
    """Insert a full-size background rect right after the opening <svg> tag."""
    if color == "none":
        return svg_text
    rect = f'<rect x="0" y="0" width="100%" height="100%" fill="{color}"/>'
    return _SVG_OPEN_RE.sub(lambda m: m.group(0) + rect, svg_text, count=1)


FAST_LOADER = Path(__file__).with_name("netlistsvg_fast.js")
NODE_STACK_KB = 500_000


def _netlistsvg_script(netlistsvg: str) -> Path | None:
    """netlistsvg's JavaScript entry point (behind a symlink, or the npm .cmd shim on Windows)."""
    p = Path(netlistsvg).resolve()
    if p.suffix.lower() in (".cmd", ".ps1", "") and not p.read_bytes()[:64].lstrip().startswith(b"#!"):
        p = p.parent / "node_modules" / "netlistsvg" / "bin" / "netlistsvg.js"
    if p.suffix == ".js" and p.is_file():
        return p
    try:
        if b"node" in p.open("rb").readline():
            return p
    except OSError:
        pass
    return None


def _netlistsvg_launcher(netlistsvg: str) -> list[str]:
    """netlistsvg through netlistsvg_fast.js (a non-recursive, indexed bus split/join search: same
    drawing, without the stack overflows and the quadratic time on wide datapaths); on POSIX
    also with a bigger Node stack for whatever else recurses deeply."""
    node = find_tool("node")
    script = _netlistsvg_script(netlistsvg) if node else None
    if not script:
        return [netlistsvg]
    cmd = [node, str(FAST_LOADER), str(script)]
    if os.name != "posix" or not shutil.which("sh"):
        return cmd
    # without a large enough OS stack, a big --stack-size would crash Node: then the default is used
    return ["sh", "-c", f'ulimit -s {NODE_STACK_KB + 50_000} 2>/dev/null && exec "$0" --stack-size={NODE_STACK_KB} '
            f'"$@" || exec "$0" "$@"', *cmd]


class DrawTimeout(VeriViewError):
    pass


# ELK options for big schematics: about 3-4x faster layout (fewer crossing-minimization passes,
# simple node placement); small schematics keep netlistsvg's default, more careful layout
FAST_LAYOUT = {
    "org.eclipse.elk.layered.thoroughness": "1",
    "org.eclipse.elk.layered.nodePlacement.strategy": "SIMPLE",
    "org.eclipse.elk.layered.crossingMinimization.strategy": "INTERACTIVE",
    "org.eclipse.elk.layered.compaction.postCompaction.strategy": "NONE",
}
_fast_skins: dict[str, Path] = {}


def fast_layout_skin(netlistsvg: str, skin: Path | None) -> Path | None:
    """The skin (the given one, else netlistsvg's default) with the fast ELK options added."""
    script = _netlistsvg_script(netlistsvg)
    base = skin or (script.parent.parent / "lib" / "default.svg" if script else None)
    if not base or not base.is_file():
        return skin
    key = str(base.resolve())
    if key not in _fast_skins:
        import hashlib
        import tempfile
        text = base.read_text(encoding="utf-8")
        m = re.search(r"<s:layoutEngine\b([^>]*?)(/?)>", text)
        if not m:
            return skin
        extra = "".join(f' {k}="{v}"' for k, v in FAST_LAYOUT.items() if k not in m.group(1))
        text = text[:m.start()] + f"<s:layoutEngine{m.group(1)}{extra}{m.group(2)}>" + text[m.end():]
        out = Path(tempfile.gettempdir()) / f"veriview-fast-{hashlib.sha1(key.encode()).hexdigest()[:10]}.svg"
        out.write_text(text, encoding="utf-8")
        _fast_skins[key] = out
    return _fast_skins[key]


def run_netlistsvg(json_path: Path, svg_path: Path, skin: Path | None = None, timeout: float | None = None,
                   fast: bool = False) -> str:
    """Render a Yosys JSON netlist with netlistsvg; returns the SVG text."""
    netlistsvg = find_tool("netlistsvg")
    if not netlistsvg:
        raise VeriViewError("netlistsvg not found (run with --install-deps).")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    if fast:
        skin = fast_layout_skin(netlistsvg, skin)
    args = [str(json_path), "-o", str(svg_path)] + (["--skin", str(skin)] if skin else [])
    launcher = _netlistsvg_launcher(netlistsvg)
    try:
        r = subprocess.run([*launcher, *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and launcher[0] != netlistsvg:   # the fast path failed: the plain netlistsvg
            log.warn(f"netlistsvg via VeriView's loader failed, retrying the plain netlistsvg: "
                     f"{(r.stderr or r.stdout).strip().splitlines()[-1:]}")
            r = subprocess.run([netlistsvg, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DrawTimeout(f"netlistsvg took longer than {timeout:.0f} s for {svg_path}") from e
    if r.returncode != 0 or not svg_path.is_file() or svg_path.stat().st_size == 0:
        raise VeriViewError(f"netlistsvg failed for {svg_path}\n{(r.stdout + r.stderr).strip()}")
    return svg_path.read_text(encoding="utf-8")


def render(json_path: Path, svg_path: Path, *, skin: Path | None = None,
           bg: str = "white", keep_json: bool = False) -> None:
    svg = run_netlistsvg(json_path, svg_path, skin)
    svg_path.write_text(add_background(svg, bg), encoding="utf-8")
    if keep_json:
        shutil.copyfile(json_path, svg_path.with_suffix(".json"))
    log.info(f"Schematic saved: {svg_path}")


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def open_url(url: str) -> None:
    """Open a URL in the default browser (Windows' browser on WSL)."""
    try:
        if _is_wsl() and shutil.which("cmd.exe"):
            subprocess.run(["cmd.exe", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        import webbrowser
        if not webbrowser.open(url):
            log.warn(f"Open {url} in your browser.")
    except OSError as e:
        log.warn(f"Could not open {url}: {e}")


def open_path(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        elif _is_wsl() and shutil.which("wslpath"):
            win = subprocess.run(["wslpath", "-w", str(path.resolve())], capture_output=True, text=True).stdout.strip()
            subprocess.run(["explorer.exe", win])  # returns 1 even on success
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            log.warn("Don't know how to open files on this system.")
    except OSError as e:
        log.warn(f"Could not open {path}: {e}")
