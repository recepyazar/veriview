"""--timing: static timing report on the command line (same engine as the viewer: timing.js)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .html import slim_netlist
from .log import VeriViewError
from .tools import find_tool

TIMING_JS = Path(__file__).with_name("timing.js")

RUNNER = r"""
const T = require(process.argv[2]);
const job = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const o = {};
for (const [k, v] of Object.entries(job.options)) {
  const ps = T.parseTime(v, k === "period" ? "ns" : "ps");
  if (ps === null || (k === "period" && ps <= 0)) {
    console.log(JSON.stringify({ error: `invalid ${k}: ${v}` }));
    process.exit(0);
  }
  o[k] = ps;
}
const r = T.analyzeTiming(job.net, o);
const ep = (e) => e && ({ name: e.name, kind: e.kind, pin: e.pin || null, arrival: e.arrival, minArrival: e.minArrival,
  required: e.required, slack: e.slack, holdSlack: e.holdSlack });
const path = (e, mode) => (e ? r.path(e, mode).map((s) => ({ kind: s.kind, type: s.type || null, net: s.net || s.name,
  delay: s.delay, arrival: s.arrival })) : []);
const holdEps = r.endpoints.filter((e) => e.holdSlack !== null).sort((a, b) => a.holdSlack - b.holdSlack);
console.log(JSON.stringify({
  lib: r.lib.name, period: r.period, inputDelay: r.inputDelay, outputDelay: r.outputDelay, uncertainty: r.uncertainty,
  area: r.area, counts: r.counts, minPeriod: r.minPeriod, fmaxMHz: r.fmaxMHz,
  wns: r.wns, tns: r.tns, setupViolations: r.setupViolations,
  worstHold: r.worstHold, holdTns: r.holdTns, holdViolations: r.holdViolations,
  unknown: r.unknown, loops: r.loopCells.length,
  endpoints: r.endpoints.slice(0, job.maxEndpoints).map(ep), totalEndpoints: r.endpoints.length,
  critical: { endpoint: ep(r.endpoints[0]), path: path(r.endpoints[0], "setup") },
  holdCritical: { endpoint: ep(holdEps[0]), path: path(holdEps[0], "hold") },
}));
"""


def analyze(module: dict[str, Any], work_dir: Path, options: dict[str, str], max_endpoints: int = 10) -> dict[str, Any]:
    node = find_tool("node")
    if not node:
        raise VeriViewError("--timing needs Node.js (it is also required by netlistsvg).")
    job = work_dir / "timing_job.json"
    job.write_text(json.dumps({"net": slim_netlist(module), "options": options, "maxEndpoints": max_endpoints}))
    runner = work_dir / "timing_runner.js"
    runner.write_text(RUNNER)
    r = subprocess.run([node, str(runner), str(TIMING_JS), str(job)], capture_output=True, text=True)
    if r.returncode != 0:
        raise VeriViewError(f"Timing analysis failed:\n{r.stderr.strip()}")
    res = json.loads(r.stdout)
    if "error" in res:
        raise VeriViewError(f"--timing: {res['error']} (e.g. 2ns, 500ps, 250MHz)")
    return res


def fmt(ps: float | None) -> str:
    if ps is None:
        return "-"
    return f"{ps / 1000:.3f} ns" if abs(ps) >= 1000 else f"{round(ps)} ps"


def format_report(res: dict[str, Any], top: str) -> str:
    lines = []
    add = lines.append
    ncells = sum(res["counts"].values())
    add(f"Timing report: {top}  (library: {res['lib']})")
    add(f"  clock period {fmt(res['period'])}, input delay {fmt(res['inputDelay'])}, "
        f"output delay {fmt(res['outputDelay'])}, uncertainty {fmt(res['uncertainty'])}")
    add("")
    add(f"  Area            {res['area']:.1f} um^2 ({ncells} cells)")
    fmax = res["fmaxMHz"]
    add(f"  Critical path   {fmt(res['minPeriod'])}" + (f"  ->  fmax {fmax:.0f} MHz" if fmax else ""))
    add(f"  Setup           WNS {fmt(res['wns'])}, TNS {fmt(res['tns'])}, {res['setupViolations']} violation(s)")
    if res["worstHold"] is None:
        add("  Hold            no registers")
    else:
        add(f"  Hold            worst {fmt(res['worstHold'])}, TNS {fmt(res['holdTns'])}, "
            f"{res['holdViolations']} violation(s)")
    if res["unknown"]:
        add(f"  WARNING: no timing model for {', '.join(res['unknown'])}")
    if res["loops"]:
        add(f"  WARNING: {res['loops']} cells in combinational loops were not timed")

    if res["endpoints"]:
        add("")
        add(f"  {'Endpoint':<28}{'Arrival':>12}{'Required':>12}{'Setup slack':>14}{'Hold slack':>13}")
        for e in res["endpoints"]:
            mark = "  VIOLATED" if e["slack"] < 0 or (e["holdSlack"] is not None and e["holdSlack"] < 0) else ""
            add(f"  {e['name']:<28}{fmt(e['arrival']):>12}{fmt(e['required']):>12}{fmt(e['slack']):>14}"
                f"{fmt(e['holdSlack']):>13}{mark}")
        if res["totalEndpoints"] > len(res["endpoints"]):
            add(f"  ... {res['totalEndpoints'] - len(res['endpoints'])} more endpoints")

    for title, key, slack_key in (("Critical (setup) path", "critical", "slack"),
                                  ("Shortest (hold) path", "holdCritical", "holdSlack")):
        crit = res[key]
        if not crit.get("endpoint"):  # e.g. no registers -> no hold path
            continue
        ep = crit["endpoint"]
        add("")
        add(f"  {title} to {ep['name']}  (slack {fmt(ep[slack_key])})")
        add(f"    {'Delay':>10}{'Arrival':>12}  Point")
        for s in crit["path"]:
            kinds = {"port": "input port", "launch": f"{s['type']} clk->Q", "undriven": "undriven"}
            what = kinds.get(s["kind"], s["type"])
            add(f"    {fmt(s['delay']):>10}{fmt(s['arrival']):>12}  {s['net']}  ({what})")
        if slack_key == "slack":
            add(f"    {'':>10}{fmt(ep['required']):>12}  required (period - setup - uncertainty)")
        else:
            add(f"    {'':>10}{fmt(ep['minArrival'] - ep['holdSlack']):>12}  required (hold + uncertainty)")
    return "\n".join(lines)
