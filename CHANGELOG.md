# Changelog

## 5.8.0

- **State machine diagrams**: FSMs are extracted with Yosys (`fsm_detect` / `fsm_extract`) and drawn in the viewer (**◇ FSM**): state names from `enum`s or `localparam`s, encodings, Moore outputs, reset state, transitions labelled with the original source expressions (merged and simplified with Quine–McCluskey). Draggable layout; in simulation mode the current state and the last transition are highlighted; links to the code panel; SVG download.
- `--fsm` prints the state machines (and writes Graphviz with `-o file.dot`).
- Several FSMs per module (selector) and links to FSMs in sub-modules; small FSMs that Yosys' recoding heuristic skips are detected too.
- New examples `fsm/traffic.sv` (Moore, enum), `fsm/seq_fsm.v` (localparam, synchronous reset) and `fsm/multi_fsm.sv` / `fsm.f` (several FSMs).

## 5.7.0

- **Gate-level timing analysis**: static timing with per-gate delays (generic illustrative library), setup and hold checks, clock period / input delay / output delay / clock uncertainty, critical path, fmax, WNS/TNS and area.
  - Viewer: **⏱ Timing** mode — gates coloured by slack with delay labels, endpoint table, click to highlight and zoom to setup/hold paths.
  - CLI: `--timing <period>` prints an STA-style report (`--input-delay`, `--output-delay`, `--uncertainty`).
- **Source code panel** in the viewer (**</> Code**): click a cell to see its source line, click a line to see its cells (across the hierarchy); sources are embedded in the HTML.
- New example `pipe_mac.sv` (pipelined multiply-accumulate).
- Viewer keeps the zoom level when the window is resized.

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
