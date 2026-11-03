/* VeriView timing engine (no DOM access).
 *
 * Static timing analysis (STA) of a gate-level netlist in the "slim" format embedded by
 * html.py: {cells: {name: [type, {port: [dir, bits]}, params?]}, ports: {name: [dir, bits]},
 * names: {name: bits}}. Units are picoseconds.
 *
 * Model (deliberately simple, single ideal clock):
 *  - every combinational gate has delay = d + k * fanout   (fanout = loads on its output)
 *  - flip-flops launch at the clock edge with clk->Q = clk2q + k * fanout
 *  - setup check at FF data pins (D, enable, synchronous reset):
 *        slack = (period - setup - uncertainty) - max arrival
 *  - hold check at the same pins:   slack = min arrival - (hold + uncertainty)
 *  - primary inputs arrive at `inputDelay`, primary outputs must be stable by
 *        period - outputDelay - uncertainty
 *  - asynchronous set/reset/load pins are not timed (no recovery/removal checks)
 *
 * The default library is a generic, illustrative one (values roughly in the range of a
 * 130 nm standard-cell library). It is not a sign-off model.
 *
 * Inlined into viewer.html by html.py; also loadable with node (module.exports).
 */

const GENERIC_LIB = {
  name: "generic (illustrative, ~130 nm)",
  // delay = d + k * fanout [ps], area [um^2]
  cells: {
    $_BUF_: { d: 25, k: 4, area: 3.75 },
    $_NOT_: { d: 15, k: 5, area: 3.75 },
    $_NAND_: { d: 20, k: 7, area: 3.75 },
    $_NOR_: { d: 25, k: 9, area: 3.75 },
    $_AND_: { d: 40, k: 5, area: 6.26 },
    $_OR_: { d: 45, k: 5, area: 6.26 },
    $_ANDNOT_: { d: 35, k: 6, area: 6.26 },
    $_ORNOT_: { d: 40, k: 6, area: 6.26 },
    $_XOR_: { d: 55, k: 8, area: 8.76 },
    $_XNOR_: { d: 55, k: 8, area: 8.76 },
    $_MUX_: { d: 50, k: 7, area: 11.26 },
    $_NMUX_: { d: 45, k: 7, area: 11.26 },
    $_AOI3_: { d: 35, k: 9, area: 6.26 },
    $_OAI3_: { d: 35, k: 9, area: 6.26 },
    $_AOI4_: { d: 40, k: 10, area: 8.76 },
    $_OAI4_: { d: 40, k: 10, area: 8.76 },
  },
  ff: { clk2q: 120, k: 5, setup: 80, hold: 30, area: 20.02, areaReset: 25.02, areaEnable: 3.75 },
};

const FF_RE = /^\$_(DFF|DFFE|SDFF|SDFFE|SDFFCE|DFFSR|DFFSRE|ALDFF|ALDFFE|DLATCH|DLATCHSR)_[PN01]+_$/;
// pins whose data is captured at the clock edge (setup/hold checked)
const ffDataPins = (type) => {
  const k = FF_RE.exec(type)[1];
  const pins = ["D"];
  if (k.includes("E") && !k.startsWith("DLATCH")) pins.push("E");
  if (k.startsWith("SDFF")) pins.push("R");
  return pins;
};
const ffClockPin = (type) => (FF_RE.exec(type)[1].startsWith("DLATCH") ? "E" : "C");

// "2ns", "500 ps", "250MHz", "1.5" (ns) -> picoseconds
function parseTime(text, defaultUnit = "ns") {
  const m = /^\s*([0-9]*\.?[0-9]+)\s*(ps|ns|us|ghz|mhz|khz)?\s*$/i.exec(String(text));
  if (!m) return null;
  const v = parseFloat(m[1]), u = (m[2] || defaultUnit).toLowerCase();
  if (u === "ps") return v;
  if (u === "ns") return v * 1e3;
  if (u === "us") return v * 1e6;
  if (u === "ghz") return v > 0 ? 1e3 / v : null;
  if (u === "mhz") return v > 0 ? 1e6 / v : null;
  if (u === "khz") return v > 0 ? 1e9 / v : null;
  return null;
}
const fmtTime = (ps) => (Math.abs(ps) >= 1000 ? `${(ps / 1000).toFixed(3)} ns` : `${Math.round(ps)} ps`);

function analyzeTiming(net, options = {}) {
  const lib = options.lib || GENERIC_LIB;
  const period = options.period ?? 2000;
  const inputDelay = options.inputDelay ?? 0;
  const outputDelay = options.outputDelay ?? 0;
  const unc = options.uncertainty ?? 0;
  const cells = Object.entries(net.cells);
  const isNum = (b) => typeof b === "number";

  // ---- connectivity ----------------------------------------------------------------
  const fanout = new Map();      // bit -> number of loads
  const addLoad = (b) => { if (isNum(b)) fanout.set(b, (fanout.get(b) || 0) + 1); };
  for (const [, [, conns]] of cells) for (const [dir, bits] of Object.values(conns)) if (dir === "input") bits.forEach(addLoad);
  for (const [, [dir, bits]] of Object.entries(net.ports)) if (dir === "output") bits.forEach(addLoad);
  const outBits = (conns) => Object.values(conns).filter(([d]) => d === "output").flatMap(([, b]) => b).filter(isNum);
  const inBits = (conns) => Object.values(conns).filter(([d]) => d === "input").flatMap(([, b]) => b).filter(isNum);

  const kind = new Map(), delay = new Map(), unknown = new Set();
  let area = 0;
  const counts = {};
  for (const [name, [type, conns]] of cells) {
    counts[type] = (counts[type] || 0) + 1;
    const fo = Math.max(1, ...outBits(conns).map((b) => fanout.get(b) || 0));
    if (lib.cells[type]) {
      kind.set(name, "comb");
      delay.set(name, lib.cells[type].d + lib.cells[type].k * fo);
      area += lib.cells[type].area;
    } else if (FF_RE.test(type)) {
      kind.set(name, "ff");
      delay.set(name, lib.ff.clk2q + lib.ff.k * fo);
      const k = FF_RE.exec(type)[1];
      area += lib.ff.area + (/[01]_$/.test(type) || k.includes("SR") || k.startsWith("SDFF") ? lib.ff.areaReset - lib.ff.area : 0)
        + (k.includes("E") && !k.startsWith("DLATCH") ? lib.ff.areaEnable : 0);
    } else if (type === "$scopeinfo") {
      kind.set(name, "ignore");
    } else {
      kind.set(name, "unknown"); unknown.add(type);
      delay.set(name, 0);
    }
  }

  // ---- forward: arrival times (max for setup, min for hold) --------------------------
  const arrMax = new Map(), arrMin = new Map(), predMax = new Map(), predMin = new Map();
  for (const [p, [dir, bits]] of Object.entries(net.ports)) {
    if (dir === "output") continue;
    bits.forEach((b) => { if (isNum(b)) { arrMax.set(b, inputDelay); arrMin.set(b, inputDelay); predMax.set(b, { port: p }); predMin.set(b, { port: p }); } });
  }
  for (const [name, [, conns]] of cells) {
    if (kind.get(name) !== "ff") continue;
    for (const b of outBits(conns)) {
      arrMax.set(b, delay.get(name)); arrMin.set(b, delay.get(name));
      predMax.set(b, { cell: name, launch: true }); predMin.set(b, { cell: name, launch: true });
    }
  }
  const combCells = cells.filter(([n]) => kind.get(n) === "comb" || kind.get(n) === "unknown");
  const driverOf = new Map();
  for (const [name, [, conns]] of combCells) for (const b of outBits(conns)) driverOf.set(b, name);
  const pending = new Map(), users = new Map();
  for (const [name, [, conns]] of combCells) {
    const deps = new Set(inBits(conns).filter((b) => driverOf.has(b)).map((b) => driverOf.get(b)));
    deps.delete(name);
    pending.set(name, deps.size);
    for (const d of deps) { if (!users.has(d)) users.set(d, []); users.get(d).push(name); }
  }
  const order = [];
  const queue = combCells.filter(([n]) => pending.get(n) === 0).map(([n]) => n);
  while (queue.length) {
    const n = queue.shift();
    order.push(n);
    for (const u of users.get(n) || []) { pending.set(u, pending.get(u) - 1); if (pending.get(u) === 0) queue.push(u); }
  }
  const loopCells = combCells.map(([n]) => n).filter((n) => pending.get(n) > 0);
  for (const name of order) {
    const conns = net.cells[name][1];
    let mx = -Infinity, mn = Infinity, pmx = null, pmn = null;
    for (const b of inBits(conns)) {
      const a = arrMax.has(b) ? arrMax.get(b) : 0, i = arrMin.has(b) ? arrMin.get(b) : 0;
      if (a > mx) { mx = a; pmx = b; }
      if (i < mn) { mn = i; pmn = b; }
    }
    if (mx === -Infinity) { mx = 0; mn = 0; }
    const d = delay.get(name);
    for (const b of outBits(conns)) {
      arrMax.set(b, mx + d); arrMin.set(b, mn + d);
      predMax.set(b, { cell: name, from: pmx }); predMin.set(b, { cell: name, from: pmn });
    }
  }

  // ---- endpoints -----------------------------------------------------------------------
  const bitName = (() => {
    const map = new Map();
    for (const [n, bits] of Object.entries(net.names)) {
      bits.forEach((b, i) => { if (isNum(b) && (!map.has(b) || n.length < map.get(b).length)) map.set(b, bits.length > 1 ? `${n}[${i}]` : n); });
    }
    for (const [n, [, bits]] of Object.entries(net.ports)) bits.forEach((b, i) => { if (isNum(b)) map.set(b, bits.length > 1 ? `${n}[${i}]` : n); });
    return (b) => map.get(b) || `net ${b}`;
  })();
  const endpoints = [];
  const setupReq = period - lib.ff.setup - unc;
  for (const [name, [type, conns]] of cells) {
    if (kind.get(name) !== "ff") continue;
    const q = outBits(conns)[0];
    const reg = q !== undefined ? bitName(q) : name;
    for (const pin of ffDataPins(type)) {
      const b = conns[pin] && conns[pin][1][0];
      if (!isNum(b)) continue;
      const amax = arrMax.has(b) ? arrMax.get(b) : 0, amin = arrMin.has(b) ? arrMin.get(b) : 0;
      endpoints.push({
        kind: "ff", cell: name, pin, bit: b, name: `${reg} (${pin})`, arrival: amax, minArrival: amin,
        required: setupReq, slack: setupReq - amax, holdSlack: amin - (lib.ff.hold + unc),
      });
    }
  }
  const outReq = period - outputDelay - unc;
  for (const [p, [dir, bits]] of Object.entries(net.ports)) {
    if (dir === "input") continue;
    bits.forEach((b, i) => {
      if (!isNum(b)) return;
      const amax = arrMax.has(b) ? arrMax.get(b) : 0;
      endpoints.push({ kind: "out", port: p, bit: b, name: bits.length > 1 ? `${p}[${i}]` : p, arrival: amax,
        minArrival: arrMin.has(b) ? arrMin.get(b) : 0, required: outReq, slack: outReq - amax, holdSlack: null });
    });
  }
  endpoints.sort((a, b) => a.slack - b.slack);

  // ---- backward: required times -> slack per cell (for colouring) ---------------------
  const reqMax = new Map();
  const tighten = (b, r) => { if (isNum(b) && (!reqMax.has(b) || r < reqMax.get(b))) reqMax.set(b, r); };
  for (const e of endpoints) tighten(e.bit, e.required);
  for (let i = order.length - 1; i >= 0; i--) {
    const name = order[i], conns = net.cells[name][1];
    let r = Infinity;
    for (const b of outBits(conns)) if (reqMax.has(b)) r = Math.min(r, reqMax.get(b));
    if (r === Infinity) continue;
    for (const b of inBits(conns)) tighten(b, r - delay.get(name));
  }
  const cellSlack = new Map();
  for (const [name, [, conns]] of cells) {
    const k = kind.get(name);
    if (k !== "comb" && k !== "ff" && k !== "unknown") continue;
    let s = Infinity;
    for (const b of outBits(conns)) if (reqMax.has(b) && arrMax.has(b)) s = Math.min(s, reqMax.get(b) - arrMax.get(b));
    if (s !== Infinity) cellSlack.set(name, s);
  }

  // ---- summary -------------------------------------------------------------------------
  const setupEps = endpoints, holdEps = endpoints.filter((e) => e.holdSlack !== null);
  const wns = setupEps.length ? Math.min(0, setupEps[0].slack) : 0;
  const tns = setupEps.reduce((s, e) => s + Math.min(0, e.slack), 0);
  const worstHold = holdEps.length ? Math.min(...holdEps.map((e) => e.holdSlack)) : null;
  const holdTns = holdEps.reduce((s, e) => s + Math.min(0, e.holdSlack), 0);
  // smallest period meeting setup on every endpoint (uncertainty ignored)
  let minPeriod = 0;
  for (const e of endpoints) minPeriod = Math.max(minPeriod, e.kind === "ff" ? e.arrival + lib.ff.setup : e.arrival + outputDelay);

  // path to an endpoint: [{kind: "port"|"launch"|"cell", name, type, delay, arrival, bit}]
  function path(ep, mode = "setup") {
    const pred = mode === "hold" ? predMin : predMax, arr = mode === "hold" ? arrMin : arrMax;
    const steps = [];
    let b = ep.bit, guard = 0;
    while (isNum(b) && guard++ < 100000) {
      const p = pred.get(b);
      if (!p) { steps.push({ kind: "undriven", name: bitName(b), bit: b, delay: 0, arrival: 0 }); break; }
      if (p.port) { steps.push({ kind: "port", name: p.port, bit: b, delay: inputDelay, arrival: arr.get(b) }); break; }
      const type = net.cells[p.cell][0];
      steps.push({ kind: p.launch ? "launch" : "cell", name: p.cell, type, bit: b, net: bitName(b), delay: delay.get(p.cell), arrival: arr.get(b) });
      if (p.launch) break;
      b = p.from;
    }
    steps.reverse();
    return steps;
  }

  return {
    lib, period, inputDelay, outputDelay, uncertainty: unc,
    delay, arrMax, arrMin, reqMax, cellSlack, kind, endpoints, path, bitName,
    area, counts, unknown: [...unknown], loopCells,
    wns, tns, worstHold, holdTns,
    setupViolations: setupEps.filter((e) => e.slack < 0).length,
    holdViolations: holdEps.filter((e) => e.holdSlack < 0).length,
    minPeriod, fmaxMHz: minPeriod > 0 ? 1e6 / minPeriod : null,
  };
}

const VVTiming = { GENERIC_LIB, analyzeTiming, parseTime, fmtTime };
if (typeof module !== "undefined" && module.exports) module.exports = VVTiming;
