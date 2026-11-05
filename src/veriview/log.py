"""Minimal colored logging to stderr."""

from __future__ import annotations

import os
import sys


class VeriViewError(Exception):
    """A user-facing error; printed without a traceback."""


_quiet = False
# extra receivers of log lines, e.g. the launcher collecting a build log: fn(level, message)
_sinks: list = []


def add_sink(fn) -> None:
    _sinks.append(fn)


def remove_sink(fn) -> None:
    if fn in _sinks:
        _sinks.remove(fn)


def set_quiet(quiet: bool) -> None:
    global _quiet
    _quiet = quiet


def _use_color() -> bool:
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def _emit(color: str, tag: str, msg: str) -> None:
    for fn in list(_sinks):
        fn(tag, msg)
    if _use_color():
        print(f"\033[{color}m[{tag}] {msg}\033[0m", file=sys.stderr)
    else:
        print(f"[{tag}] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    if not _quiet:
        _emit("0;32", "INFO", msg)


def warn(msg: str) -> None:
    _emit("1;33", "WARN", msg)


def error(msg: str) -> None:
    _emit("0;31", "ERROR", msg)
