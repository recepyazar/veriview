/* VeriView simulation engine (no DOM access).
 *
 * Two-valued (0/1), cycle-based simulation of a hierarchical Yosys netlist in the
 * "slim" format embedded by html.py:  module -> {cells: {name: [type, {port: [dir, bits]}, params?]},
 * ports: {name: [dir, bits]}, names: {...}}. Undriven, x and z bits read as 0. Registers
 * start at 0 and update on the active edge of their own clock net; asynchronous
 * resets/sets/loads and latches act immediately. Sub-module instances are simulated
 * recursively, each with its own register state (keyed by instance path "/a/b").
 *
 * Inlined into viewer.html by html.py; also loadable with node (module.exports) for tests.
 */

const rd = (vals, bits) => bits.map((b) => (typeof b === "number" ? vals.get(b) || 0 : b === "1" ? 1 : 0));
const bitsOfParam = (s, w) => { const b = String(s || "").split("").reverse().map((c) => (c === "1" ? 1 : 0)); return ext(b, w, false); };
const pInt = (cell, k, d = 0) => {
  const v = cell[2] && cell[2][k];
  if (v === undefined) return d;
  return /^[01xz]+$/.test(v) ? parseInt(v.replace(/[xz]/g, "0"), 2) : Number(v) || d;
};
const toBig = (v) => { let r = 0n; for (let i = v.length - 1; i >= 0; i--) r = (r << 1n) | BigInt(v[i]); return r; };
const toBigS = (v) => (v.length && v[v.length - 1] ? toBig(v) - (1n << BigInt(v.length)) : toBig(v));
const fromBig = (x, w) => { const o = []; for (let i = 0; i < w; i++) o.push(Number((x >> BigInt(i)) & 1n)); return o; };
const ext = (v, w, signed) => { const f = signed && v.length ? v[v.length - 1] : 0; const o = v.slice(0, w); while (o.length < w) o.push(f); return o; };
const any = (v) => v.some((x) => x);

// value formatting / parsing
const fmtVal = (bits) => {
  if (bits.length === 1) return String(bits[0]);
  const v = toBig(bits);
  return `${bits.length}'h${v.toString(16).toUpperCase()}` + (bits.length <= 16 ? ` (${v})` : "");
};
const parseVal = (text, w) => {
  const t = text.trim().replace(/_/g, "");
  let m, v;
  if ((m = /^(?:\d+)?'?([hbdo])([0-9a-fA-F]+)$/.exec(t))) v = BigInt({ h: "0x", b: "0b", o: "0o", d: "" }[m[1].toLowerCase()] + m[2]);
  else if (/^-?(0x[0-9a-f]+|0b[01]+|0o[0-7]+|\d+)$/i.test(t)) v = t.startsWith("-") ? -BigInt(t.slice(1)) : BigInt(t);
  else return null;
  return fromBig(v, w);
};

function createSimulator(netOf, isModule) {
  const sim = {
    top: null, inputs: new Map(), regs: new Map(), mems: new Map(), vals: new Map(), ffCache: new Map(),
    dirty: new Map(),       // instance key -> bits changed since its last evaluation
    seen: new Map(),        // instance key -> which cells were evaluated at least once
    regsAt: new Map(),      // instance key -> register state by cell position (no per-pass string keys)
    unsupported: new Set(), cycle: 0, loopWarn: false,
    userSet: new Set(),     // inputs the user changed (others get defaults on reset)
    resetPorts: new Map(),  // top-level input -> inactive level, for inputs that drive register resets
    held: [],               // [{port, level, count}] registers currently held in reset
  };
  const hierNet = netOf;

  function evalComb(type, cell, vals) {
    const c = cell[1];
    const g = (p) => (c[p] ? rd(vals, c[p][1]) : []);
    const W = (p) => (c[p] ? c[p][1].length : 0);
    const yw = W("Y"), As = pInt(cell, "A_SIGNED"), Bs = pInt(cell, "B_SIGNED");
    const num = (v, s) => (s ? toBigS(v) : toBig(v));
    const bool = (x) => fromBig(x ? 1n : 0n, yw);
    const shiftBy = (a, amt, w, fill) => { const o = []; for (let i = 0; i < w; i++) { const j = i + amt; o.push(j >= 0 && j < a.length ? a[j] : j >= a.length ? fill : 0); } return o; };
    switch (type) {
      case "$buf": case "$pos": return { Y: ext(g("A"), yw, As) };
      case "$not": return { Y: ext(g("A"), yw, As).map((b) => 1 - b) };
      case "$neg": return { Y: fromBig(-num(g("A"), As), yw) };
      case "$and": case "$or": case "$xor": case "$xnor": {
        const s = As && Bs, a = ext(g("A"), yw, s), b = ext(g("B"), yw, s);
        return { Y: a.map((x, i) => (type === "$and" ? x & b[i] : type === "$or" ? x | b[i] : type === "$xor" ? x ^ b[i] : 1 - (x ^ b[i]))) };
      }
      case "$reduce_and": return { Y: bool(g("A").every((x) => x)) };
      case "$reduce_or": case "$reduce_bool": return { Y: bool(any(g("A"))) };
      case "$reduce_xor": return { Y: bool(g("A").reduce((p, x) => p ^ x, 0)) };
      case "$reduce_xnor": return { Y: bool(!g("A").reduce((p, x) => p ^ x, 0)) };
      case "$logic_not": return { Y: bool(!any(g("A"))) };
      case "$logic_and": return { Y: bool(any(g("A")) && any(g("B"))) };
      case "$logic_or": return { Y: bool(any(g("A")) || any(g("B"))) };
      case "$eq": case "$eqx": case "$ne": case "$nex": case "$lt": case "$le": case "$gt": case "$ge": {
        const s = As && Bs, w = Math.max(W("A"), W("B"));
        const a = num(ext(g("A"), w, s), s), b = num(ext(g("B"), w, s), s);
        return { Y: bool({ $eq: a === b, $eqx: a === b, $ne: a !== b, $nex: a !== b, $lt: a < b, $le: a <= b, $gt: a > b, $ge: a >= b }[type]) };
      }
      case "$add": case "$sub": case "$mul": case "$div": case "$mod": case "$divfloor": case "$modfloor": case "$pow": {
        const s = As && Bs, w = Math.max(W("A"), W("B"), yw);
        const a = num(ext(g("A"), w, s), s), b = num(ext(g("B"), w, s), s);
        let r;
        if (type === "$add") r = a + b; else if (type === "$sub") r = a - b; else if (type === "$mul") r = a * b;
        else if (type === "$pow") r = b < 0n ? 0n : b > 4096n ? 0n : a ** b;
        else if (b === 0n) r = -1n; // x in Yosys; all ones here
        else if (type === "$div") r = a / b;
        else if (type === "$mod") r = a % b;
        else if (type === "$divfloor") r = a / b - (a % b !== 0n && (a < 0n) !== (b < 0n) ? 1n : 0n);
        else r = ((a % b) + b) % b;
        return { Y: fromBig(r, yw) };
      }
      case "$shl": case "$sshl": case "$shr": case "$sshr": case "$shift": case "$shiftx": {
        const aw = Math.max(W("A"), yw);
        const a = type === "$shiftx" || type === "$shift" ? g("A") : ext(g("A"), aw, As);
        let amt = num(g("B"), (type === "$shift" || type === "$shiftx") && Bs);
        if (amt > 1000000n) amt = 1000000n; if (amt < -1000000n) amt = -1000000n;
        const n = Number(amt);
        if (type === "$shl" || type === "$sshl") return { Y: shiftBy(a, -n, yw, 0) };
        const fill = type === "$sshr" && As ? a[a.length - 1] || 0 : 0;
        return { Y: shiftBy(a, n, yw, fill) };
      }
      case "$mux": return { Y: g("S")[0] ? g("B") : g("A") };
      case "$pmux": {
        const w = W("A"), S = g("S"), B = g("B");
        const i = S.indexOf(1);
        return { Y: i < 0 ? g("A") : B.slice(i * w, (i + 1) * w) };
      }
      case "$bmux": { const w = W("Y"), i = Number(toBig(g("S"))); return { Y: ext(g("A").slice(i * w, (i + 1) * w), w, false) }; }
      case "$demux": { const w = W("A"), i = Number(toBig(g("S"))), y = new Array(yw).fill(0); g("A").forEach((b, k) => { y[i * w + k] = b; }); return { Y: y }; }
      case "$bwmux": { const A = g("A"), B = g("B"), S = g("S"); return { Y: A.map((x, i) => (S[i] ? B[i] : x)) }; }
      case "$tribuf": return { Y: g("EN")[0] ? g("A") : new Array(yw).fill(0) };
      case "$lut": { const lut = bitsOfParam(cell[2] && cell[2].LUT, 1 << W("A")); return { Y: [lut[Number(toBig(g("A")))] || 0] }; }
      // gate level
      case "$_BUF_": return { Y: g("A") };
      case "$_NOT_": return { Y: [1 - g("A")[0]] };
      case "$_AND_": return { Y: [g("A")[0] & g("B")[0]] };
      case "$_NAND_": return { Y: [1 - (g("A")[0] & g("B")[0])] };
      case "$_OR_": return { Y: [g("A")[0] | g("B")[0]] };
      case "$_NOR_": return { Y: [1 - (g("A")[0] | g("B")[0])] };
      case "$_XOR_": return { Y: [g("A")[0] ^ g("B")[0]] };
      case "$_XNOR_": return { Y: [1 - (g("A")[0] ^ g("B")[0])] };
      case "$_ANDNOT_": return { Y: [g("A")[0] & (1 - g("B")[0])] };
      case "$_ORNOT_": return { Y: [g("A")[0] | (1 - g("B")[0])] };
      case "$_MUX_": return { Y: [g("S")[0] ? g("B")[0] : g("A")[0]] };
      case "$_NMUX_": return { Y: [1 - (g("S")[0] ? g("B")[0] : g("A")[0])] };
      case "$_AOI3_": return { Y: [1 - ((g("A")[0] & g("B")[0]) | g("C")[0])] };
      case "$_OAI3_": return { Y: [1 - ((g("A")[0] | g("B")[0]) & g("C")[0])] };
      case "$_AOI4_": return { Y: [1 - ((g("A")[0] & g("B")[0]) | (g("C")[0] & g("D")[0]))] };
      case "$_OAI4_": return { Y: [1 - ((g("A")[0] | g("B")[0]) & (g("C")[0] | g("D")[0]))] };
      case "$scopeinfo": case "$print": case "$check": case "$assert": case "$assume": case "$cover": case "$live": case "$fair":
        return {};
    }
    return null;
  }

  // Normalized register description: clk/en/arst/srst/set/clr/aload/latch = [port, activeLevel, (value)]
  function ffDesc(type, cell) {
    const pol = (ch) => (ch === "P" ? 1 : 0), val = (ch) => [ch === "1" ? 1 : 0];
    let m = /^\$_(DFF|DFFE|SDFF|SDFFE|SDFFCE|DFFSR|DFFSRE|DLATCH|DLATCHSR|ALDFF|ALDFFE)_([PN01]+)_$/.exec(type);
    if (m) {
      const [, k, L] = m, d = { w: 1 };
      if (k === "DLATCH") { d.latch = ["E", pol(L[0])]; if (L.length === 3) d.arst = ["R", pol(L[1]), val(L[2])]; return d; }
      if (k === "DLATCHSR") { d.latch = ["E", pol(L[0])]; d.set = ["S", pol(L[1])]; d.clr = ["R", pol(L[2])]; return d; }
      d.clk = ["C", pol(L[0])];
      if (k === "DFF" && L.length === 3) d.arst = ["R", pol(L[1]), val(L[2])];
      if (k === "DFFE") { if (L.length === 4) { d.arst = ["R", pol(L[1]), val(L[2])]; d.en = ["E", pol(L[3])]; } else d.en = ["E", pol(L[1])]; }
      if (k.startsWith("SDFF")) { d.srst = ["R", pol(L[1]), val(L[2])]; if (L.length === 4) d.en = ["E", pol(L[3])]; d.ce = k === "SDFFCE"; }
      if (k.startsWith("DFFSR")) { d.set = ["S", pol(L[1])]; d.clr = ["R", pol(L[2])]; if (L.length === 4) d.en = ["E", pol(L[3])]; }
      if (k.startsWith("ALDFF")) { d.aload = ["L", pol(L[1]), "AD"]; if (L.length === 3) d.en = ["E", pol(L[2])]; }
      return d;
    }
    m = /^\$(dff|dffe|adff|adffe|sdff|sdffe|sdffce|dffsr|dffsre|aldff|aldffe|dlatch|adlatch|dlatchsr)$/.exec(type);
    if (!m) return null;
    const k = m[1], w = pInt(cell, "WIDTH", 1), d = { w };
    const isLatch = k.includes("latch");
    if (isLatch) d.latch = ["EN", pInt(cell, "EN_POLARITY", 1)];
    else d.clk = ["CLK", pInt(cell, "CLK_POLARITY", 1)];
    if (!isLatch && k.endsWith("e")) d.en = ["EN", pInt(cell, "EN_POLARITY", 1)];
    if (k === "adff" || k === "adffe" || k === "adlatch") d.arst = ["ARST", pInt(cell, "ARST_POLARITY", 1), bitsOfParam(cell[2].ARST_VALUE, w)];
    if (k.startsWith("sdff")) { d.srst = ["SRST", pInt(cell, "SRST_POLARITY", 1), bitsOfParam(cell[2].SRST_VALUE, w)]; d.ce = k === "sdffce"; }
    if (k.startsWith("dffsr") || k === "dlatchsr") { d.set = ["SET", pInt(cell, "SET_POLARITY", 1)]; d.clr = ["CLR", pInt(cell, "CLR_POLARITY", 1)]; }
    if (k.startsWith("aldff")) d.aload = ["ALOAD", pInt(cell, "ALOAD_POLARITY", 1), "AD"];
    return d;
  }
  const ffOf = (modName, name, cell) => {
    const k = modName + "\u0000" + name;
    if (!sim.ffCache.has(k)) sim.ffCache.set(k, ffDesc(cell[0], cell));
    return sim.ffCache.get(k);
  };
  const active = (spec, sigs) => (sigs(spec[0])[0] || 0) === spec[1];
  function asyncQ(d, q, sigs) {
    let out = q;
    if (d.latch && active(d.latch, sigs)) out = ext(sigs("D"), d.w, false);
    if (d.aload && active(d.aload, sigs)) out = ext(sigs(d.aload[2]), d.w, false);
    if (d.set || d.clr) {
      const S = d.set ? sigs(d.set[0]) : [], R = d.clr ? sigs(d.clr[0]) : [];
      out = out.map((b, i) => (R.length && R[i] === d.clr[1] ? 0 : S.length && S[i] === d.set[1] ? 1 : b));
    }
    if (d.arst && active(d.arst, sigs)) out = d.arst[2].slice();
    return out;
  }
  function syncNext(d, q, sigs) {
    if (d.srst && !d.ce && active(d.srst, sigs)) return d.srst[2].slice();
    if (d.en && !active(d.en, sigs)) return q;
    if (d.srst && active(d.srst, sigs)) return d.srst[2].slice();
    return ext(sigs("D"), d.w, false);
  }

  // ---- memories ($mem_v2, as made by memory_collect) -----------------------------------
  // words: arrays of bits; read ports without a clock are combinational, clocked read ports are
  // registers updated on their clock edge (read-before-write unless the port is transparent);
  // write ports write on their clock edge, bit by bit (WR_EN), in port order.
  function memDesc(cell) {
    const p = (k, d = 0) => pInt(cell, k, d);
    const bitsP = (k, n) => bitsOfParam(cell[2] && cell[2][k], n);
    const R = p("RD_PORTS"), Wp = p("WR_PORTS"), w = p("WIDTH", 1), ab = p("ABITS", 1);
    return {
      size: p("SIZE"), off: p("OFFSET"), w, ab, R, W: Wp,
      rdClk: bitsP("RD_CLK_ENABLE", R), rdPol: bitsP("RD_CLK_POLARITY", R), rdCeSrst: bitsP("RD_CE_OVER_SRST", R),
      rdTrans: bitsP("RD_TRANSPARENCY_MASK", R * Wp), rdInit: bitsP("RD_INIT_VALUE", R * w),
      rdArst: bitsP("RD_ARST_VALUE", R * w), rdSrst: bitsP("RD_SRST_VALUE", R * w),
      wrClk: bitsP("WR_CLK_ENABLE", Wp), wrPol: bitsP("WR_CLK_POLARITY", Wp), init: cell[2] && cell[2].INIT,
    };
  }
  function memState(key, name, cell, mod) {
    const mk = key + "\u0000" + name;
    let m = sim.mems.get(mk);
    if (!m) {
      const d = memDesc(cell), init = bitsOfParam(d.init, d.size * d.w);
      const words = [];
      for (let i = 0; i < d.size; i++) words.push(init.slice(i * d.w, (i + 1) * d.w));
      m = { d, words, rdq: Array.from({ length: d.R }, (_, i) => d.rdInit.slice(i * d.w, (i + 1) * d.w)),
        prevRd: new Array(d.R).fill(null), prevWr: new Array(d.W).fill(null), key, name, mod };
      sim.mems.set(mk, m);
    }
    return m;
  }
  const slice = (v, i, n) => v.slice(i * n, (i + 1) * n);
  const memRead = (m, addr) => { const i = Number(toBig(addr)) - m.d.off; return i >= 0 && i < m.d.size ? m.words[i].slice() : new Array(m.d.w).fill(0); };
  function memOutputs(m, sigs) {
    const d = m.d, A = sigs("RD_ADDR"), arst = sigs("RD_ARST"), out = [];
    for (let i = 0; i < d.R; i++) {
      if (!d.rdClk[i]) out.push(...memRead(m, slice(A, i, d.ab)));
      else {
        if (arst[i]) m.rdq[i] = slice(d.rdArst, i, d.w);
        out.push(...m.rdq[i]);
      }
    }
    return out;
  }
  // the effect of clock edges on a memory: [] if none, else a function applying it
  function memEdges(m, sigs) {
    const d = m.d, rclk = sigs("RD_CLK"), wclk = sigs("WR_CLK"), acts = [];
    const fired = (prev, i, c, pol) => { const e = prev[i] !== null && prev[i] !== c && c === pol; prev[i] = c; return e; };
    const writes = [];
    const WA = sigs("WR_ADDR"), WD = sigs("WR_DATA"), WE = sigs("WR_EN");
    for (let j = 0; j < d.W; j++) {
      if (!d.wrClk[j] || !fired(m.prevWr, j, wclk[j] || 0, d.wrPol[j])) continue;
      writes.push([Number(toBig(slice(WA, j, d.ab))) - d.off, slice(WD, j, d.w), slice(WE, j, d.w), j]);
    }
    const RA = sigs("RD_ADDR"), EN = sigs("RD_EN"), SR = sigs("RD_SRST");
    for (let i = 0; i < d.R; i++) {
      if (!d.rdClk[i] || !fired(m.prevRd, i, rclk[i] || 0, d.rdPol[i])) continue;
      const en = EN.length ? EN[i] : 1;
      if (SR[i] && (!d.rdCeSrst[i] || en)) { acts.push(() => { m.rdq[i] = slice(d.rdSrst, i, d.w); }); continue; }
      if (!en) continue;
      const addr = Number(toBig(slice(RA, i, d.ab))) - d.off;
      let data = memRead(m, slice(RA, i, d.ab));
      for (const [wa, wd, we, j] of writes) {   // transparent port: sees this edge's write
        if (wa === addr && d.rdTrans[i * d.W + j]) data = data.map((b, k) => (we[k] ? wd[k] : b));
      }
      acts.push(() => { m.rdq[i] = data; });
    }
    for (const [wa, wd, we] of writes) {
      if (wa < 0 || wa >= d.size) continue;
      acts.push(() => { m.words[wa] = m.words[wa].map((b, k) => (we[k] ? wd[k] : b)); });
    }
    return acts;
  }

  // Evaluation order of a module's cells: registers and memories first (their outputs are state),
  // then the other cells topologically (drivers before readers). minReader[bit] = the first
  // position reading a bit: a pass only has to be repeated when a cell changes a bit that an
  // earlier cell has already read (a loop, or a path through a sub-module); usually one pass.
  const orderCache = new Map();
  function orderOf(modName) {
    if (orderCache.has(modName)) return orderCache.get(modName);
    const n = hierNet(modName), entries = Object.entries(n.cells);
    const isState = ([name, cell]) => cell[0] === "$mem_v2" || !!ffOf(modName, name, cell);
    const ins = (cell) => {
      const out = [];
      for (const [p, [dir, bits]] of Object.entries(cell[1])) {
        if (dir === "output" || (cell[0] === "$mem_v2" && p === "RD_DATA") || p === "Q") continue;
        for (const b of bits) if (typeof b === "number") out.push(b);
      }
      return out;
    };
    const outs = (cell) => {
      const out = [];
      for (const [p, [dir, bits]] of Object.entries(cell[1])) {
        if (dir === "input" && !(cell[0] === "$mem_v2" && p === "RD_DATA")) continue;
        if (dir === "" && p !== "Y" && p !== "Q" && p !== "RD_DATA") continue;
        for (const b of bits) if (typeof b === "number") out.push(b);
      }
      return out;
    };
    const driver = new Map();
    entries.forEach((e, i) => { for (const b of outs(e[1])) driver.set(b, i); });
    const deg = new Array(entries.length).fill(0), succ = entries.map(() => []);
    entries.forEach((e, i) => {
      if (isState(e)) return;
      const seen = new Set();
      for (const b of ins(e[1])) {
        const d = driver.get(b);
        if (d === undefined || d === i || seen.has(d) || isState(entries[d])) continue;
        seen.add(d); succ[d].push(i); deg[i]++;
      }
    });
    const order = [], placed = new Array(entries.length).fill(false);
    entries.forEach((e, i) => { if (isState(e)) { order.push(i); placed[i] = true; } });
    const queue = [];
    entries.forEach((e, i) => { if (!placed[i] && !deg[i]) queue.push(i); });
    for (let q = 0; q < queue.length; q++) {
      const i = queue[q]; order.push(i); placed[i] = true;
      for (const j of succ[i]) if (--deg[j] === 0) queue.push(j);
    }
    entries.forEach((e, i) => { if (!placed[i]) order.push(i); });   // on a loop: settled by extra passes
    const cells = order.map((i) => entries[i]), minReader = new Map();
    cells.forEach(([, cell], pos) => { for (const b of ins(cell)) if (!minReader.has(b)) minReader.set(b, pos); });
    // combinational cells are only re-evaluated when one of their input bits changed
    const gated = cells.map(([name, cell]) => !isState([name, cell]) && !isModule(cell[0]));
    const insOf = cells.map(([, cell]) => ins(cell));
    const ffd = cells.map(([name, cell]) => ffOf(modName, name, cell));
    const isMod = cells.map(([, cell]) => isModule(cell[0]));
    const o = { cells, minReader, gated, insOf, ffd, isMod };
    orderCache.set(modName, o);
    return o;
  }

  function evalModule(modName, key, inVals, depth = 0) {
    let vals = sim.vals.get(key);
    if (!vals) { vals = new Map(); sim.vals.set(key, vals); }
    let dirty = sim.dirty.get(key);
    if (!dirty) { dirty = new Set(); sim.dirty.set(key, dirty); }
    for (const [b, v] of inVals) if (vals.get(b) !== v) { vals.set(b, v); dirty.add(b); }
    if (depth > 64) return vals;
    const { cells, minReader, gated, insOf, ffd, isMod } = orderOf(modName);
    let seen = sim.seen.get(key);
    if (!seen) { seen = new Uint8Array(cells.length); sim.seen.set(key, seen); }
    let regsAt = sim.regsAt.get(key);
    if (!regsAt) { regsAt = []; sim.regsAt.set(key, regsAt); }
    for (let pass = 0; pass < 200; pass++) {
      let again = false, pos = 0;
      const write = (bits, v) => bits.forEach((b, i) => {
        if (typeof b === "number" && vals.get(b) !== (v[i] || 0)) {
          vals.set(b, v[i] || 0);
          dirty.add(b);
          const r = minReader.get(b);
          if (r !== undefined && r <= pos) again = true;   // read before it changed: evaluate again
        }
      });
      for (; pos < cells.length; pos++) {
        if (gated[pos] && seen[pos]) {
          const bs = insOf[pos];
          let hit = false;
          for (let k = 0; k < bs.length; k++) if (dirty.has(bs[k])) { hit = true; break; }
          if (!hit) continue;
        }
        seen[pos] = 1;
        const [name, cell] = cells[pos];
        const [type, conns] = cell;
        const ff = ffd[pos];
        if (ff) {
          let reg = regsAt[pos];
          if (!reg) {
            const rk = key + "\u0000" + name;
            reg = sim.regs.get(rk);
            if (!reg) { reg = { q: new Array(ff.w).fill(0), prev: null, d: ff, key, mod: modName, name }; sim.regs.set(rk, reg); }
            regsAt[pos] = reg;
          }
          // only flip-flops with asynchronous controls (or latches) change outside a clock edge
          if (ff.latch || ff.aload || ff.set || ff.clr || ff.arst) {
            const sigs = (p) => (conns[p] ? rd(vals, conns[p][1]) : []);
            reg.q = asyncQ(ff, reg.q, sigs);
          }
          if (conns.Q) write(conns.Q[1], reg.q);
          continue;
        }
        if (type === "$mem_v2") {
          const sigs = (p) => (conns[p] ? rd(vals, conns[p][1]) : []);
          const m = memState(key, name, cell, modName);
          if (conns.RD_DATA) write(conns.RD_DATA[1], memOutputs(m, sigs));
          continue;
        }
        if (isMod[pos]) {
          const child = hierNet(type), cin = new Map();
          for (const [p, [dir, pb]] of Object.entries(child.ports)) {
            if (dir === "output" || !conns[p]) continue;
            const v = rd(vals, conns[p][1]);
            pb.forEach((b, i) => { if (typeof b === "number") cin.set(b, v[i] || 0); });
          }
          const cv = evalModule(type, key + "/" + name, cin, depth + 1);
          for (const [p, [dir, pb]] of Object.entries(child.ports)) if (dir !== "input" && conns[p]) write(conns[p][1], rd(cv, pb));
          continue;
        }
        const out = evalComb(type, cell, vals);
        if (!out) { sim.unsupported.add(type); continue; }
        for (const [p, v] of Object.entries(out)) if (conns[p]) write(conns[p][1], v);
      }
      if (!again) { dirty.clear(); return vals; }
    }
    sim.loopWarn = true; // combinational loop that does not settle
    return vals;
  }

  const topInputBits = () => {
    const n = hierNet(sim.top), m = new Map();
    for (const [p, [dir, pb]] of Object.entries(n.ports)) {
      if (dir === "output") continue;
      const v = sim.inputs.get(p) || [];
      pb.forEach((b, i) => { if (typeof b === "number") m.set(b, v[i] || 0); });
    }
    return m;
  };
  const settle = () => evalModule(sim.top, "", topInputBits());
  // Settle, then fire registers whose clock net saw its active edge; repeat for derived clocks.
  function simUpdate() {
    sim.loopWarn = false;
    settle();
    for (let guard = 0; guard < 32; guard++) {
      const updates = [];
      for (const reg of sim.regs.values()) {
        if (!reg.d.clk) continue;
        const vals = sim.vals.get(reg.key), conns = hierNet(reg.mod).cells[reg.name][1];
        const sigs = (p) => (conns[p] ? rd(vals, conns[p][1]) : []);
        const c = sigs(reg.d.clk[0])[0] || 0;
        if (reg.prev !== null && reg.prev !== c && c === reg.d.clk[1]) updates.push([reg, syncNext(reg.d, reg.q, sigs)]);
        reg.prev = c;
      }
      const memActs = [];
      for (const m of sim.mems.values()) {
        const vals = sim.vals.get(m.key), cell = hierNet(m.mod).cells[m.name];
        const conns = cell[1], sigs = (p) => (conns[p] ? rd(vals, conns[p][1]) : []);
        memActs.push(...memEdges(m, sigs));
      }
      if (!updates.length && !memActs.length) break;
      for (const [reg, q] of updates) reg.q = q;
      for (const act of memActs) act();
      settle();
    }
    sim.held = heldInReset();
  }

  // ---- reset analysis: which top-level input drives each register's reset ----------
  const modAt = (path) => path.reduce((m, inst) => (m && hierNet(m).cells[inst] ? hierNet(m).cells[inst][0] : null), sim.top);
  // Follow a bit of module `mod` (instance key "/a/b") up through the ports to a top-level input.
  function traceToTop(key, mod, bit) {
    const path = key.split("/").filter(Boolean);
    let cur = mod, b = bit;
    for (let i = path.length; i > 0; i--) {
      if (typeof b !== "number") return null;
      const port = Object.entries(hierNet(cur).ports).find(([, [, pb]]) => pb.includes(b));
      if (!port) return null;
      const [pname, [, pb]] = port, parent = modAt(path.slice(0, i - 1));
      const conn = parent && hierNet(parent).cells[path[i - 1]][1][pname];
      if (!conn) return null;
      b = conn[1][pb.indexOf(b)];
      cur = parent;
    }
    if (typeof b !== "number") return null;
    const top = Object.entries(hierNet(sim.top).ports).find(([, [dir, pb]]) => dir !== "output" && pb.includes(b));
    return top ? top[0] : null;
  }
  const resetSpecs = (d) => [d.arst, d.srst, d.clr && [d.clr[0], d.clr[1]], d.set && [d.set[0], d.set[1]]].filter(Boolean);
  function findResetPorts() {
    const out = new Map();
    for (const reg of sim.regs.values()) {
      const conns = hierNet(reg.mod).cells[reg.name][1];
      for (const spec of resetSpecs(reg.d)) {
        const bits = conns[spec[0]] ? conns[spec[0]][1] : [];
        const port = traceToTop(reg.key, reg.mod, bits[0]);
        if (port && hierNet(sim.top).ports[port][1].length === 1) out.set(port, 1 - spec[1]);
      }
    }
    return out;
  }
  function heldInReset() {
    const groups = new Map();
    for (const reg of sim.regs.values()) {
      const vals = sim.vals.get(reg.key), conns = hierNet(reg.mod).cells[reg.name][1];
      const sigs = (p) => (conns[p] ? rd(vals, conns[p][1]) : []);
      for (const spec of [reg.d.arst, reg.d.srst].filter(Boolean)) {
        if (!active(spec, sigs)) continue;
        const port = traceToTop(reg.key, reg.mod, (conns[spec[0]] || [0, []])[1][0]);
        const k = port || "";
        const g = groups.get(k) || { port, level: spec[1], count: 0 };
        g.count++; groups.set(k, g);
        break;
      }
    }
    return [...groups.values()];
  }
  // Reset all registers to 0. Inputs the user set are kept (keepInputs); all others start
  // at 0, except inputs that drive a register reset, which start inactive (e.g. rst_n = 1).
  function simReset(keepInputs = true) {
    sim.regs.clear(); sim.mems.clear(); sim.vals.clear(); sim.dirty.clear(); sim.seen.clear(); sim.regsAt.clear();
    sim.ffCache.clear(); sim.unsupported.clear(); sim.cycle = 0;
    if (!keepInputs) { sim.inputs.clear(); sim.userSet.clear(); }
    const n = hierNet(sim.top), fresh = [];
    for (const [p, [dir, pb]] of Object.entries(n.ports)) {
      if (dir === "output") continue;
      const cur = sim.inputs.get(p);
      if (!sim.userSet.has(p) || !cur || cur.length !== pb.length) {
        sim.inputs.set(p, new Array(pb.length).fill(0)); sim.userSet.delete(p); fresh.push(p);
      }
    }
    simUpdate();
    sim.resetPorts = findResetPorts();
    let changed = false;
    for (const p of fresh) {
      if (sim.resetPorts.has(p)) { sim.inputs.set(p, [sim.resetPorts.get(p)]); changed = true; }
    }
    if (changed) simUpdate();
  }
  // One clock cycle = active edge then inactive edge on input port `clk`.
  function clockCycles(clk, n) {
    const w = (hierNet(sim.top).ports[clk] || [0, [0]])[1].length;
    for (let i = 0; i < n; i++) {
      sim.inputs.set(clk, new Array(w).fill(1)); simUpdate();
      sim.inputs.set(clk, new Array(w).fill(0)); simUpdate();
      sim.cycle++;
    }
  }
  // Run a stimulus from a fresh state. steps[t] = {port: bits} applied at the start of
  // cycle t (missing ports keep their previous value). Cycle t starts with a rising
  // clock edge (for t > 0, if `clk` is set), then the inputs of cycle t are applied and the
  // design settles; probes are sampled at that point. If stopAt is given, simulation
  // stops after sampling cycle stopAt (the design state then shows that cycle).
  // Returns {name: [bits per cycle]}.
  function runWave(clk, steps, probes, stopAt = steps.length - 1) {
    const userSet = new Set(sim.userSet), saved = new Map(sim.inputs);
    simReset(false);
    const w = clk && hierNet(sim.top).ports[clk] ? hierNet(sim.top).ports[clk][1].length : 1;
    const rec = Object.fromEntries(Object.keys(probes).map((k) => [k, []]));
    for (let t = 0; t <= stopAt && t < steps.length; t++) {
      if (clk && t > 0) {
        sim.inputs.set(clk, new Array(w).fill(1)); simUpdate();
        sim.inputs.set(clk, new Array(w).fill(0));
        sim.cycle++;
      }
      for (const [p, bits] of Object.entries(steps[t] || {})) if (p !== clk) sim.inputs.set(p, bits);
      simUpdate();
      const vals = sim.values("");
      for (const [k, bits] of Object.entries(probes)) rec[k].push(rd(vals, bits));
    }
    sim.userSet = userSet;
    for (const [p, v] of saved) if (userSet.has(p)) sim.inputs.set(p, v);
    return rec;
  }
  sim.runWave = runWave;
  sim.reset = simReset;
  sim.update = simUpdate;
  sim.setInput = (p, bits) => { sim.inputs.set(p, bits); sim.userSet.add(p); simUpdate(); };
  sim.clock = clockCycles;
  sim.values = (key = "") => sim.vals.get(key) || new Map();
  return sim;
}


// Value Change Dump for a waveform: signals = [{name, values: [bits per cycle]}].
// Cycle t spans [t*period, (t+1)*period); a clock with a rising edge at the start of
// every cycle t > 0 is added when clkName is given.
function toVCD(signals, { clkName = null, period = 10, top = "top" } = {}) {
  const ids = [];
  const id = (i) => { let s = ""; i += 1; while (i > 0) { s = String.fromCharCode(33 + ((i - 1) % 94)) + s; i = Math.floor((i - 1) / 94); } return s; };
  const all = signals.map((sg, i) => ({ ...sg, id: id(i + 1), width: sg.values.length ? sg.values[0].length : 1 }));
  const clkId = id(0);
  const safe = (n) => n.replace(/\s+/g, "_");
  const out = ["$date VeriView $end", "$version VeriView simulation $end", "$timescale 1ns $end", `$scope module ${safe(top)} $end`];
  if (clkName) out.push(`$var wire 1 ${clkId} ${safe(clkName)} $end`);
  for (const sg of all) out.push(`$var wire ${sg.width} ${sg.id} ${safe(sg.name)}${sg.width > 1 ? ` [${sg.width - 1}:0]` : ""} $end`);
  out.push("$upscope $end", "$enddefinitions $end");
  const n = Math.max(0, ...all.map((sg) => sg.values.length));
  const fmt = (sg, bits) => (sg.width === 1 ? `${bits[0]}${sg.id}` : `b${bits.slice().reverse().join("")} ${sg.id}`);
  const last = new Map();
  for (let t = 0; t < n; t++) {
    const lines = [];
    if (clkName && t > 0) lines.push(`1${clkId}`);
    if (clkName && t === 0) lines.push(`0${clkId}`);
    for (const sg of all) {
      const v = sg.values[t]; if (!v) continue;
      const key = v.join("");
      if (last.get(sg.id) !== key) { lines.push(fmt(sg, v)); last.set(sg.id, key); }
    }
    if (lines.length) out.push(`#${t * period}`, ...lines);
    if (clkName && t > 0) out.push(`#${t * period + period / 2}`, `0${clkId}`);
  }
  out.push(`#${n * period}`);
  return out.join("\n") + "\n";
}

const VVSim = { createSimulator, rd, fmtVal, parseVal, toBig, fromBig, any, toVCD };
if (typeof module !== "undefined" && module.exports) module.exports = VVSim;
