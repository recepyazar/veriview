# VeriView roadmap

## 1. Releasing on PyPI (`pip install veriview`)

Goal: anyone can run `pipx install veriview` / `uv tool install veriview` / `pip install veriview`.

**Already in place**

- [x] Python package with a `veriview` console script, no Python dependencies
- [x] Package metadata: description, classifiers, project URLs, MIT license (PEP 639)
- [x] README rewritten for PyPI at build time (relative images/links → GitHub URLs)
- [x] Lean sdist (no `results/`, `docs/`, `.github/`); wheel contains `viewer.html` and `sim.js`
- [x] `twine check --strict` passes; the wheel installs and runs in a clean virtualenv
- [x] `CHANGELOG.md`
- [x] `.github/workflows/publish.yml`: Trusted Publishing (no API tokens), version/tag check, TestPyPI on manual run, PyPI on GitHub release
- [x] The name `veriview` is still free on PyPI (checked 2026-11-03)

**One-time setup (needs the maintainer's accounts)**

1. Create accounts on <https://pypi.org> and <https://test.pypi.org> and enable 2FA (required).
2. Register the GitHub workflow as a *pending trusted publisher* on **both** sites
   (Account settings → Publishing → "Add a new pending publisher" → GitHub):

   | Field | pypi.org | test.pypi.org |
   | :--- | :--- | :--- |
   | PyPI project name | `veriview` | `veriview` |
   | Owner | `recepyazar` | `recepyazar` |
   | Repository name | `veriview` | `veriview` |
   | Workflow name | `publish.yml` | `publish.yml` |
   | Environment name | `pypi` | `testpypi` |

   A pending publisher also reserves the project name until the first upload.
3. On GitHub: Settings → Environments → create `pypi` and `testpypi`
   (optionally add yourself as required reviewer for `pypi`, so every release needs one click of approval).

**First release**

4. Dry run on TestPyPI: Actions → *Publish* → *Run workflow* → target `testpypi`. Then check
   <https://test.pypi.org/project/veriview/> (README, images) and a real install:
   `pipx install --index-url https://test.pypi.org/simple/ veriview` → `veriview --version`.
5. Release: `gh release create v5.6.0 --title "VeriView 5.6.0" --notes-file CHANGELOG.md`
   (or via the GitHub web UI). The workflow checks that the tag matches `__version__` and uploads to PyPI.
6. Verify: `pipx install veriview && veriview --install-deps && veriview --version`.

**Every later release**

- Bump `__version__` in `src/veriview/__init__.py` (PyPI never accepts the same version twice), add a `CHANGELOG.md` entry, merge to `main`, create release `vX.Y.Z`.

**After the first release**

- [ ] Add a PyPI version badge to the README; mention `pipx install veriview` first in *Installation*.
- [ ] Optional: `conda-forge` recipe (conda users often already have Yosys from `litex-hub`).

## 2. Near term (polish)

- [ ] PNG/PDF export (`--format png|pdf`, via `rsvg-convert` when available).
- [ ] Simulation: memories (`$mem_v2`), X-propagation (4-valued) as an option, read a VCD as stimulus.
- [ ] Waveform: signals from sub-modules, zoom, markers / delta between two cycles.
- [ ] Viewer performance for large designs (lazy rendering of views, web worker for simulation).
- [ ] Docker image with Yosys, netlistsvg, sv2v and VeriView (`docker run … veriview design.sv`).
- [ ] Windows: test natively (no WSL) in CI.

## 3. IC design flow view (bigger steps)

Ordered so that each step is useful on its own and builds on the previous one.

1. **Area and timing estimates (post-synthesis)**
   Map the gate-level netlist to an open standard-cell library (SkyWater **sky130**), report cell count and area (µm²),
   run **OpenSTA** for the critical path and an estimated fmax, and highlight the critical path in the viewer
   (reusing the cone-highlighting machinery). Rough power estimate from switching activity of the waveform simulation.
   Needs: a Liberty file (~tens of MB, downloaded on demand) and OpenSTA.
2. **"Metrics" panel in the viewer** for area / timing / power per module, comparable between the RTL, optimized and gate views.
3. **Place & route + layout view**
   Drive **OpenROAD** (via OpenLane 2 or OpenROAD-flow-scripts, preferably in Docker) with the sky130 PDK, parse its reports
   (utilization, post-route timing, power) and show a layout image in a new *Layout* tab. Separate, optional command
   (e.g. `veriview flow design.f --pdk sky130`), because it needs several GB of PDK and minutes per run.
4. **Lightweight FPGA alternative**: **nextpnr** (iCE40 / ECP5), pip-installable via the `yowasp-*` packages —
   fmax, LUT/FF usage and placement within seconds.

## 4. Ecosystem

- [ ] VS Code extension: "Show schematic" / "Simulate" on a `.v`/`.sv` file (wraps the CLI + viewer).
- [ ] GitHub Action: render schematics of changed modules in pull requests.
- [ ] `veriview serve`: long-running local server that re-synthesizes on demand (enables live `--depth`/`--cone` and larger designs).
