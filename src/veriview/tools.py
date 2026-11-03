"""Locating and installing external tools (yosys, netlistsvg, sv2v)."""

from __future__ import annotations

import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import log
from .log import VeriViewError

SV2V_VERSION = "v0.0.13"


def local_bin_dir() -> Path:
    """Where VeriView installs tools that don't need root (sv2v)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "veriview" / "bin"
    return Path.home() / ".local" / "bin"


def find_tool(name: str) -> str | None:
    """Find a tool on PATH, falling back to VeriView's local bin dir."""
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=str(local_bin_dir()))


def slang_available(yosys: str) -> bool:
    try:
        r = subprocess.run([yosys, "-q", "-p", "plugin -i slang"], capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


MIN_YOSYS = (0, 40)


def yosys_version(yosys: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run([yosys, "-V"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Yosys (\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def warn_if_old_yosys() -> None:
    yosys = find_tool("yosys")
    ver = yosys_version(yosys) if yosys else None
    if ver and ver < MIN_YOSYS:
        log.warn(
            f"Yosys {ver[0]}.{ver[1]} is old; some features (e.g. --cone with --flatten) may fail. "
            "Install a recent build, e.g. the OSS CAD Suite: https://github.com/YosysHQ/oss-cad-suite-build"
        )


# --- Installation -------------------------------------------------------------

def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower().startswith("y")


def _run(cmd: list[str]) -> None:
    if os.name == "posix" and os.geteuid() != 0 and cmd[0] in ("apt-get", "dnf", "pacman", "npm"):
        cmd = ["sudo"] + cmd
    log.info("$ " + " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        raise VeriViewError(f"Command failed: {' '.join(cmd)}")


def install_system_packages(yosys: bool, node: bool) -> None:
    if sys.platform == "darwin":
        if not shutil.which("brew"):
            raise VeriViewError("Homebrew is required on macOS: https://brew.sh")
        if yosys:
            _run(["brew", "install", "yosys"])
        if node:
            _run(["brew", "install", "node"])
        return
    if sys.platform == "win32":
        raise VeriViewError(
            "Automatic installation is not supported on Windows. Install the OSS CAD Suite "
            "(https://github.com/YosysHQ/oss-cad-suite-build) and Node.js, then: npm install -g netlistsvg"
        )
    pkgs = (["yosys"] if yosys else []) + (["nodejs", "npm"] if node else [])
    if shutil.which("apt-get"):
        _run(["apt-get", "update"])
        _run(["apt-get", "install", "-y"] + pkgs)
    elif shutil.which("dnf"):
        _run(["dnf", "install", "-y"] + pkgs)
    elif shutil.which("pacman"):
        _run(["pacman", "-S", "--needed", "--noconfirm"] + pkgs)
    else:
        raise VeriViewError(f"Unsupported package manager. Please install manually: {' '.join(pkgs)}")


def install_netlistsvg() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise VeriViewError("npm not found; install Node.js first.")
    prefix = subprocess.run([npm, "prefix", "-g"], capture_output=True, text=True).stdout.strip()
    cmd = [npm, "install", "-g", "netlistsvg"]
    if os.name == "posix" and prefix and not os.access(prefix, os.W_OK) and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    log.info("$ " + " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        raise VeriViewError("netlistsvg installation failed.")


def install_sv2v() -> Path:
    system = platform.system()
    asset_os = {"Linux": "Linux", "Darwin": "macOS", "Windows": "Windows"}.get(system)
    if not asset_os:
        raise VeriViewError(f"No prebuilt sv2v for {system}. See https://github.com/zachjs/sv2v")
    url = f"https://github.com/zachjs/sv2v/releases/download/{SV2V_VERSION}/sv2v-{asset_os}.zip"
    exe = "sv2v.exe" if system == "Windows" else "sv2v"
    dest_dir = local_bin_dir()
    dest = dest_dir / exe
    log.info(f"Downloading sv2v {SV2V_VERSION} -> {dest}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/" + exe) or n == exe)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))
    except (OSError, StopIteration, zipfile.BadZipFile) as e:
        raise VeriViewError(f"Failed to install sv2v from {url}: {e}") from e
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if shutil.which(exe) is None:
        log.warn(f"{dest_dir} is not in PATH (VeriView will still find sv2v there).")
    return dest


def ensure_dependencies(install: bool, assume_yes: bool, want_sv2v: bool) -> None:
    need_yosys = find_tool("yosys") is None
    need_nsvg = find_tool("netlistsvg") is None
    need_node = need_nsvg and shutil.which("npm") is None
    need_sv2v = want_sv2v and find_tool("sv2v") is None

    missing = [n for n, m in (("yosys", need_yosys), ("netlistsvg", need_nsvg), ("sv2v", need_sv2v)) if m]
    if not missing:
        return
    if not (install or _confirm(f"Missing: {' '.join(missing)}. Install now?", assume_yes)):
        raise VeriViewError(
            f"Missing dependencies: {' '.join(missing)}. Re-run with --install-deps to install them."
        )
    if need_yosys or need_node:
        install_system_packages(need_yosys, need_node)
    if need_nsvg:
        install_netlistsvg()
    if need_sv2v:
        install_sv2v()
