from pathlib import Path

import pytest

from veriview.tools import find_tool, slang_available

ROOT = Path(__file__).resolve().parent.parent
EX = ROOT / "ex_design"

HAVE_TOOLS = bool(find_tool("yosys") and find_tool("netlistsvg"))
HAVE_SV = HAVE_TOOLS and bool(find_tool("sv2v") or slang_available(find_tool("yosys")))

needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="needs yosys and netlistsvg")
needs_sv = pytest.mark.skipif(not HAVE_SV, reason="needs sv2v or yosys-slang")


@pytest.fixture(autouse=True)
def _no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
