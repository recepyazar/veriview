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


def run_netlistsvg(json_path: Path, svg_path: Path, skin: Path | None = None) -> str:
    """Render a Yosys JSON netlist with netlistsvg; returns the SVG text."""
    netlistsvg = find_tool("netlistsvg")
    if not netlistsvg:
        raise VeriViewError("netlistsvg not found (run with --install-deps).")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [netlistsvg, str(json_path), "-o", str(svg_path)]
    if skin:
        cmd += ["--skin", str(skin)]
    r = subprocess.run(cmd, capture_output=True, text=True)
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
