# Changelog

## 5.6.0 — first PyPI release

- **Simulation mode** in the HTML viewer: toggle inputs, clock the design, watch 1-valued wires light up, also inside sub-modules; works on the RTL, optimized and gate views. Inputs that drive register resets start inactive, and registers held in reset are reported.
- **Waveform panel**: draw the input stimulus per cycle by hand, see outputs and any added signals instantly, inspect a cycle in the schematic, export VCD.

## 5.4.1

- Fixes for older Yosys versions (e.g. Ubuntu's 0.33): `--split` and labels of parameterized modules; warning for Yosys < 0.40.
- CI runs on the OSS CAD Suite.

## 5.4.0

- `--watch`: rebuild on every change; with `--html` a local live-reload server (errors shown as a banner).
- When all frontends fail, every frontend's error is reported.

## 5.3.0

- Viewer: switch between RTL / optimized / gate views, flatten any module, click a net to highlight its fan-in / fan-out cone, signal search.

## 5.2.0

- `--stage elab|opt|gate`, `--select <instance>`, `--depth N`, `--cone <signal>`.

## 5.1.0

- `--html`: self-contained interactive viewer with hierarchy navigation, zoom/pan, deep links.

## 5.0.0

- Rewritten as a Python package (`veriview` command, `pip install`); `veriview.sh` kept as a wrapper.

## 4.0.0

- Robust CLI (quoting, argument checks), sv2v / yosys-slang frontends for full SystemVerilog, `.f` file list features, tests and CI.
- **Breaking:** `-f` means *file list*; use `--flatten` to flatten.
