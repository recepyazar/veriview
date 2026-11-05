# Changelog

## 6.0.0

- **Simpler commands — the viewer is the default**: `veriview design.sv` writes `design.html` and opens it in the browser (when no `-o` is given in a terminal). The output kind follows `-o` (`.html` viewer / `.svg` schematic); `--svg` for an SVG without naming the file. **Breaking:** previously the default was an SVG.
- **Launcher page**: `veriview` without arguments (or `--ui [folder]`) opens a local web page to pick files (folder browser, *Use this folder*), the top module (candidates detected automatically), views, language and options, then opens live viewers or shows timing / FSM reports; Turkish and English. Listens on 127.0.0.1 only, restricted to the start folder, protected against cross-site requests.
- **Gate editor** (`--editor`, or *✎ Gate editor* in the launcher): draw circuits from ports, constants, gates (2–4 inputs), MUX and flip-flops, wire pin to pin, and get readable Verilog-2001 live (with warnings for unconnected inputs, undriven outputs and combinational loops); *Save & open in viewer* writes the `.v` file and opens it for simulation/timing; multi-selection (click, Shift/Ctrl+click, selection box), Ctrl+C/X/V/D/A, Delete, undo/redo (Ctrl+Z/Y), pan with Space+drag; **names for gates and wires** (the Verilog net names, checked for validity and clashes) in a selection panel that also sets input counts and constants; designs saved in the browser or as files; Turkish and English.
- **Folders as input**: `veriview rtl/` uses the folder's `.f` file list, or all `.v`/`.sv` files in it. `-t NAME` (optional) picks any module as the top; with several possible tops VeriView names the one it picked and lists the other candidates.
- `--open` / `--no-open`; quick-start examples at the top of `--help`.
- **Türkçe arayüz**: the viewer is available in Turkish (automatic for a Turkish browser, **TR/EN** button, `--lang tr`); a test makes sure every UI string has a translation.

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
