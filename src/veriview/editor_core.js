/* VeriView gate editor core (no DOM access): pins and widths of the symbols and
 * Verilog generation. Inlined into editor.html by the launcher; also loadable with
 * node (module.exports) for tests.
 *
 * design: {nodes: [{id, type, name?, label?, n?, k?, bits?, value?, x, y}], wires: [{from: {node, pin}, to: {node, pin}}]}
 *   name:  port / net label name; label: optional name of the net a gate / block drives
 *   bits:  width. Ports (in), constants and blocks have their own width (default 1 / 4);
 *          gates, MUX, flip-flops, outputs and labels take the width of what drives them
 *          unless `bits` is set.
 * types:
 *   in, out, const0, const1, const (N-bit, value), tag (net label: all tags with the same name are one
 *   net, like Logisim tunnels; a wire *into* a tag drives it, wires *out of* it read it)
 *   not, buf, and/or/nand/nor/xor/xnor (n = 2..4 inputs), mux (A, B, S -> Y = S ? B : A),
 *   dff (D, CLK -> Q), dffr (+ RST_N: active-low asynchronous reset)
 *   blocks: add (A + B + CIN -> S, COUT), sub (A - B -> Y, BOUT), cmp (A, B -> EQ, LT, GT; unsigned),
 *           reg (D, CLK, EN, RST_N -> Q), cnt (CLK, EN, RST_N -> Q = Q + 1), dec (A -> Y = one-hot, 2^bits),
 *           muxn (D0..D{k-1}, S -> Y), split (A -> B0..B{bits-1}), merge (B0..B{bits-1} -> Y; B0 = LSB)
 *   EN, RST_N and CIN of blocks may stay unconnected (EN = 1, no reset, CIN = 0).
 */
const GATE2 = ["and", "or", "nand", "nor", "xor", "xnor"];
const PORT_TYPES = ["in", "out", "const0", "const1", "const", "tag"];
const SEQ_TYPES = ["dff", "dffr", "reg", "cnt"];
const BLOCK_TYPES = ["add", "sub", "cmp", "reg", "cnt", "dec", "muxn", "split", "merge"];
// gates etc. whose width follows their data inputs (unless `bits` is set)
const AUTO = new Set(["out", "tag", "not", "buf", "mux", "dff", "dffr", ...GATE2]);
const DATA_PINS = { out: ["A"], not: ["A"], buf: ["A"], mux: ["A", "B"], dff: ["D"], dffr: ["D"] };
const OPTIONAL = new Set(["CIN", "EN", "RST_N"]);
const clampBits = (b, lo = 1, hi = 64) => Math.max(lo, Math.min(hi, Math.round(+b) || lo));
const selBits = (k) => Math.max(1, Math.ceil(Math.log2(k)));
const range = (n) => Array.from({ length: n }, (_, i) => i);

function defaultBits(nd) {
  if (nd.type === "dec") return 2;
  if (BLOCK_TYPES.includes(nd.type) || nd.type === "const") return 4;
  return 1;
}
// pins by name (widths: see analyze())
function pinsOf(nd) {
  const n = nd.bits ? clampBits(nd.bits) : defaultBits(nd);
  switch (nd.type) {
    case "in": case "const0": case "const1": case "const": return { ins: [], outs: ["Y"] };
    case "tag": return { ins: [], outs: [] };   // one bidirectional pin "T", handled by the editor
    case "out": return { ins: ["A"], outs: [] };
    case "not": case "buf": return { ins: ["A"], outs: ["Y"] };
    case "mux": return { ins: ["A", "B", "S"], outs: ["Y"] };
    case "dff": return { ins: ["D", "CLK"], outs: ["Q"] };
    case "dffr": return { ins: ["D", "CLK", "RST_N"], outs: ["Q"] };
    case "add": return { ins: ["A", "B", "CIN"], outs: ["S", "COUT"] };
    case "sub": return { ins: ["A", "B"], outs: ["Y", "BOUT"] };
    case "cmp": return { ins: ["A", "B"], outs: ["EQ", "LT", "GT"] };
    case "reg": return { ins: ["D", "CLK", "EN", "RST_N"], outs: ["Q"] };
    case "cnt": return { ins: ["CLK", "EN", "RST_N"], outs: ["Q"] };
    case "dec": return { ins: ["A"], outs: ["Y"] };
    case "muxn": return { ins: [...range(clampBits(nd.k || 4, 2, 16)).map((i) => "D" + i), "S"], outs: ["Y"] };
    case "split": return { ins: ["A"], outs: range(clampBits(n, 2, 32)).map((i) => "B" + i) };
    case "merge": return { ins: range(clampBits(n, 2, 32)).map((i) => "B" + i), outs: ["Y"] };
    default: return { ins: ["A", "B", "C", "D"].slice(0, nd.n || 2), outs: ["Y"] };
  }
}
const ident = (s) => (/^[A-Za-z_][A-Za-z0-9_]*$/.test(s || "") ? s : "m_" + String(s || "x").replace(/\W/g, "_"));

// widths and drivers of a design: {width(nd), outW(nd, pin), inW(nd, pin), srcWire(nd, pin), tagDrivers}
function analyze(D) {
  const byId = Object.fromEntries(D.nodes.map((n) => [n.id, n]));
  const tagDrivers = new Map();
  for (const w of D.wires) {
    const tn = byId[w.to.node];
    if (tn && tn.type === "tag") { if (!tagDrivers.has(tn.name)) tagDrivers.set(tn.name, []); tagDrivers.get(tn.name).push(w); }
  }
  const into = new Map();
  for (const w of D.wires) into.set(`${w.to.node}:${w.to.pin}`, w);
  // the wire that really drives (nd, pin): follows net labels to their driver
  const srcWire = (nd, pin) => {
    let w = into.get(`${nd.id}:${pin}`);
    for (let hop = 0; hop < 20 && w; hop++) {
      const from = byId[w.from.node];
      if (!from || from.type !== "tag") return w;
      const ds = tagDrivers.get(from.name) || [];
      w = ds.length ? ds[0] : null;
    }
    return null;
  };
  const memo = new Map(), busy = new Set();
  function width(nd) {
    if (!nd) return 1;
    if (memo.has(nd.id)) return memo.get(nd.id);
    let v;
    if (nd.type === "const0" || nd.type === "const1") v = 1;
    else if (nd.bits) v = clampBits(nd.bits);
    else if (!AUTO.has(nd.type)) v = defaultBits(nd);
    else if (busy.has(nd.id)) return 1;   // a loop through auto-width nodes
    else {
      busy.add(nd.id);
      v = 1;
      if (nd.type === "tag") {
        const ds = tagDrivers.get(nd.name) || [];
        if (ds.length) v = outW(byId[ds[0].from.node], ds[0].from.pin);
      } else {
        for (const pin of DATA_PINS[nd.type] || ["A", "B", "C", "D"]) {
          const w = srcWire(nd, pin);
          if (w && byId[w.from.node]) { v = outW(byId[w.from.node], w.from.pin); break; }
        }
      }
      busy.delete(nd.id);
    }
    memo.set(nd.id, v);
    return v;
  }
  function outW(nd, pin) {
    if (!nd) return 1;
    const t = nd.type, n = width(nd);
    if ((t === "add" && pin === "COUT") || (t === "sub" && pin === "BOUT") || t === "cmp" || t === "split") return 1;
    if (t === "dec") return 2 ** clampBits(n, 1, 6);
    return n;
  }
  function inW(nd, pin) {
    const t = nd.type;
    if (["CLK", "RST_N", "EN", "CIN"].includes(pin) || t === "merge") return 1;
    if (pin === "S") return t === "muxn" ? selBits(clampBits(nd.k || 4, 2, 16)) : 1;
    return width(nd);
  }
  return { byId, width, outW, inW, srcWire, tagDrivers };
}

// t(message, vars): translation hook (identity by default)
function toVerilog(D, moduleName, t = (s, v) => { let o = s; for (const [k, x] of Object.entries(v || {})) o = o.split(`{${k}}`).join(String(x)); return o; }) {
  const warn = [];
  const mod = ident(String(moduleName || "").trim() || "my_design");
  const A = analyze(D), byId = A.byId;
  const ins = D.nodes.filter((n) => n.type === "in"), outs = D.nodes.filter((n) => n.type === "out");
  const names = new Set();
  for (const p of [...ins, ...outs]) { if (names.has(p.name)) warn.push(t("duplicate port name {name}", { name: p.name })); names.add(p.name); }
  // net names: user labels where valid and unique, otherwise <type><id> / q<id>
  const isId = (s) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(s || "");
  const KEYWORDS = new Set("module endmodule input output inout wire reg assign always posedge negedge begin end if else case endcase".split(" "));
  const taken = new Set([...ins, ...outs].map((p) => p.name));
  const labelOf = new Map();
  for (const n of D.nodes) {
    if (!n.label || PORT_TYPES.includes(n.type)) continue;
    if (!isId(n.label) || KEYWORDS.has(n.label)) { warn.push(t("invalid name {name} (letters, digits, _; not a keyword)", { name: n.label })); continue; }
    if (taken.has(n.label)) { warn.push(t("name {name} is used twice", { name: n.label })); continue; }
    taken.add(n.label); labelOf.set(n.id, n.label);
  }
  for (const tg of D.nodes.filter((n) => n.type === "tag")) {
    if (!isId(tg.name) || KEYWORDS.has(tg.name)) warn.push(t("invalid name {name} (letters, digits, _; not a keyword)", { name: tg.name }));
  }
  // a label names the net of its (single) driving gate, unless that gate has its own name
  for (const [name, ds] of A.tagDrivers) {
    const d = ds.length === 1 ? byId[ds[0].from.node] : null;
    if (d && !PORT_TYPES.includes(d.type) && !labelOf.has(d.id) && isId(name) && !KEYWORDS.has(name) && !taken.has(name)
        && ds[0].from.pin === pinsOf(d).outs[0] && d.type !== "cmp" && d.type !== "split") {
      labelOf.set(d.id, name); taken.add(name);
    }
  }
  const tagWarned = new Set();
  const srcWire = (nd, pin) => {
    const w0 = D.wires.find((x) => x.to.node === nd.id && x.to.pin === pin);
    const from = w0 && byId[w0.from.node];
    if (from && from.type === "tag") {
      const ds = A.tagDrivers.get(from.name) || [];
      if (ds.length !== 1 && !tagWarned.has(from.name)) {
        tagWarned.add(from.name);
        warn.push(t(ds.length ? "label {name} has several drivers" : "label {name} has no driver (tied to 0)", { name: from.name }));
      }
    }
    return A.srcWire(nd, pin);
  };
  const W = (nd) => A.width(nd);
  const zero = (n) => (n === 1 ? "1'b0" : `${n}'d0`);
  const decl = (n) => (n === 1 ? "" : `[${n - 1}:0] `);
  const nodeName = (nd) => `${nd.type === "dffr" ? "DFF+R" : nd.type.toUpperCase()}${nd.id}`;
  // the base name of a node's nets; secondary outputs get _<pin> appended
  const base = (nd) => labelOf.get(nd.id) || ((nd.type === "dff" || nd.type === "dffr") ? `q${nd.id}` : `${nd.type}${nd.id}`);
  const PRIMARY = { add: "S", sub: "Y", reg: "Q", cnt: "Q", dec: "Y", muxn: "Y", merge: "Y" };
  const net = (nd, pin) => {
    if (nd.type === "in") return ident(nd.name);
    if (nd.type === "const0") return "1'b0";
    if (nd.type === "const1") return "1'b1";
    if (nd.type === "const") { const n = W(nd); return `${n}'d${Math.max(0, Math.floor(+nd.value || 0)) % 2 ** Math.min(n, 52)}`; }
    if (nd.type === "split") return `${base(nd)}[${+String(pin || "B0").slice(1)}]`;
    if (nd.type === "cmp") return `${base(nd)}_${String(pin || "EQ").toLowerCase()}`;
    if (PRIMARY[nd.type] && pin && pin !== PRIMARY[nd.type]) return `${base(nd)}_${pin.toLowerCase()}`;
    return base(nd);
  };
  // the expression driving (nd, pin); null for an unconnected optional pin of a block
  const drv = (nd, pin) => {
    const w = srcWire(nd, pin);
    const from = w && byId[w.from.node];
    if (!from) {
      if (isOptional(nd, pin)) return null;
      if (nd.type === "out") warn.push(t("output {name} is not driven (tied to 0)", { name: nd.name }));
      else warn.push(t("unconnected input {pin} of {node} (tied to 0)", { pin, node: nodeName(nd) }));
      return zero(nd.type === "out" ? W(nd) : A.inW(nd, pin));
    }
    const a = A.outW(from, w.from.pin), b = nd.type === "out" ? W(nd) : A.inW(nd, pin);
    if (a !== b) warn.push(t("width mismatch: {a} bits into {pin} of {node} ({b} bits)", { a, b, pin, node: nd.type === "out" ? nd.name : nodeName(nd) }));
    return net(from, w.from.pin);
  };
  const gates = D.nodes.filter((n) => !PORT_TYPES.includes(n.type));
  const comb = gates.filter((n) => !SEQ_TYPES.includes(n.type)), ffs = gates.filter((n) => SEQ_TYPES.includes(n.type));
  const OP = { and: " & ", or: " | ", xor: " ^ ", nand: " & ", nor: " | ", xnor: " ^ " };
  const splitName = (g) => net(g, "B0").replace(/\[\d+\]$/, "");
  // declarations, grouped by width
  const wires = [], regs = [];
  for (const g of comb) {
    if (g.type === "split") wires.push([splitName(g), W(g)]);
    else for (const pin of pinsOf(g).outs) wires.push([net(g, pin), A.outW(g, pin)]);
  }
  for (const f of ffs) regs.push([net(f, "Q"), W(f)]);
  const declLines = (kw, list) => {
    const by = new Map();
    for (const [nm, n] of list) { if (!by.has(n)) by.set(n, []); by.get(n).push(nm); }
    return [...by.entries()].sort((x, y) => x[0] - y[0]).map(([n, nms]) => `  ${kw} ${decl(n)}${nms.join(", ")};`);
  };
  const L = [];
  L.push("// Generated by the VeriView gate editor");
  const ports = [...ins.map((p) => `  input  wire ${decl(W(p))}${ident(p.name)}`), ...outs.map((p) => `  output wire ${decl(W(p))}${ident(p.name)}`)];
  L.push(`module ${mod} (`, ports.join(",\n") || "  // " + t("no ports yet"), ");");
  if (wires.length) L.push("", ...declLines("wire", wires));
  if (regs.length) L.push(...declLines("reg ", regs));
  if (comb.length) L.push("");
  for (const g of comb) {
    const ty = g.type;
    if (ty === "add") {
      const a = drv(g, "A"), b = drv(g, "B"), c = drv(g, "CIN");
      L.push(`  assign {${net(g, "COUT")}, ${net(g, "S")}} = ${a} + ${b}${c ? ` + ${c}` : ""};`);
    } else if (ty === "sub") {
      L.push(`  assign {${net(g, "BOUT")}, ${net(g, "Y")}} = {1'b0, ${drv(g, "A")}} - {1'b0, ${drv(g, "B")}};`);
    } else if (ty === "cmp") {
      const a = drv(g, "A"), b = drv(g, "B");
      L.push(`  assign ${net(g, "EQ")} = ${a} == ${b};`, `  assign ${net(g, "LT")} = ${a} < ${b};`, `  assign ${net(g, "GT")} = ${a} > ${b};`);
    } else if (ty === "dec") {
      L.push(`  assign ${net(g, "Y")} = ${A.outW(g, "Y")}'d1 << ${drv(g, "A")};`);
    } else if (ty === "muxn") {
      const k = clampBits(g.k || 4, 2, 16), sb = selBits(k), s = drv(g, "S");
      const d = range(k).map((i) => drv(g, "D" + i));
      const e = d.slice(0, -1).map((x, i) => `(${s} == ${sb}'d${i}) ? ${x}`).join(" : ") + ` : ${d[k - 1]}`;
      L.push(`  assign ${net(g, "Y")} = ${e};`);
    } else if (ty === "merge") {
      L.push(`  assign ${net(g, "Y")} = {${range(W(g)).reverse().map((i) => drv(g, "B" + i)).join(", ")}};`);
    } else if (ty === "split") {
      L.push(`  assign ${splitName(g)} = ${drv(g, "A")};`);
    } else {
      const p = pinsOf(g).ins.map((pin) => drv(g, pin));
      let e;
      if (ty === "not") e = `~${p[0]}`;
      else if (ty === "buf") e = p[0];
      else if (ty === "mux") e = `${p[2]} ? ${p[1]} : ${p[0]}`;
      else { e = p.join(OP[ty]); if (ty.startsWith("n") || ty === "xnor") e = `~(${e})`; }
      L.push(`  assign ${net(g)} = ${e};`);
    }
  }
  for (const f of ffs) {
    const q = net(f, "Q"), n = W(f), clk = drv(f, "CLK");
    const d = f.type === "cnt" ? `${q} + 1'b1` : drv(f, "D");
    const rst = f.type === "dff" ? null : drv(f, "RST_N");
    const en = f.type === "reg" || f.type === "cnt" ? drv(f, "EN") : null;
    const upd = en ? `if (${en}) ${q} <= ${d};` : `${q} <= ${d};`;
    L.push("");
    if (rst) L.push(`  always @(posedge ${clk} or negedge ${rst})`, `    if (!${rst}) ${q} <= ${zero(n)};`, `    else ${upd}`);
    else L.push(`  always @(posedge ${clk})`, `    ${upd}`);
  }
  if (outs.length) L.push("");
  for (const o of outs) L.push(`  assign ${ident(o.name)} = ${drv(o, "A")};`);
  L.push("", "endmodule", "");
  // combinational loops (through gates and combinational blocks; registers break loops)
  const succ = new Map(comb.map((g) => [g.id, []]));
  for (const g of comb) {
    for (const pin of pinsOf(g).ins) {
      const w = A.srcWire(g, pin);
      if (w && succ.has(w.from.node)) succ.get(w.from.node).push(g.id);
    }
  }
  const state = new Map(), stack = [];
  const dfs = (v) => {
    state.set(v, 1); stack.push(v);
    for (const u of succ.get(v)) {
      if (state.get(u) === 1) warn.push(t("combinational loop through {list}", { list: stack.slice(stack.indexOf(u)).map((id) => nodeName(byId[id])).join(" → ") }));
      else if (!state.get(u)) dfs(u);
    }
    stack.pop(); state.set(v, 2);
  };
  for (const g of comb) if (!state.get(g.id)) dfs(g.id);
  return { text: L.join("\n"), warn, mod };
}
const isOptional = (nd, pin) => OPTIONAL.has(pin) && BLOCK_TYPES.includes(nd.type);

/* Simulation of a drawing, without Yosys: two-valued (unconnected inputs are 0, exactly as in the
 * generated Verilog), values as BigInt. Registers start at 0 and change on a rising edge of their
 * CLK net (any net: clocks from gates work too); active-low resets act immediately.
 *   const s = createSim(D); s.setInput(id, 1n); s.clock(clkId); s.value(nodeId, pin); s.update(D2)
 */
function createSim(D0) {
  let D, A, byId, comb, ffs, order;
  const inputs = new Map(), regs = new Map(), prevClk = new Map(), out = new Map();
  const mask = (w) => (1n << BigInt(w)) - 1n;
  const sim = { cycle: 0, unstable: false };
  function build(Dn) {
    D = Dn; A = analyze(D); byId = A.byId;
    const gates = D.nodes.filter((n) => !PORT_TYPES.includes(n.type));
    comb = gates.filter((n) => !SEQ_TYPES.includes(n.type)); ffs = gates.filter((n) => SEQ_TYPES.includes(n.type));
    // topological order of the combinational nodes (nodes on loops at the end; passes repeat until stable)
    const deg = new Map(comb.map((g) => [g.id, 0])), succ = new Map(comb.map((g) => [g.id, []]));
    for (const g of comb) for (const pin of pinsOf(g).ins) {
      const w = A.srcWire(g, pin);
      if (w && succ.has(w.from.node)) { succ.get(w.from.node).push(g.id); deg.set(g.id, deg.get(g.id) + 1); }
    }
    order = []; const q = comb.filter((g) => !deg.get(g.id)).map((g) => g.id), seen = new Set();
    while (q.length) { const id = q.shift(); order.push(byId[id]); seen.add(id); for (const u of succ.get(id)) { deg.set(u, deg.get(u) - 1); if (!deg.get(u)) q.push(u); } }
    for (const g of comb) if (!seen.has(g.id)) order.push(g);
    for (const id of [...inputs.keys()]) if (!byId[id]) inputs.delete(id);
    for (const id of [...regs.keys()]) if (!byId[id]) { regs.delete(id); prevClk.delete(id); }
  }
  const W = (nd) => A.width(nd);
  // value at an input pin (masked to the pin's width)
  const val = (nd, pin) => {
    const w = A.srcWire(nd, pin), from = w && byId[w.from.node];
    if (!from) return isOptional(nd, pin) && pin !== "CIN" ? 1n : 0n;
    const v = out.get(`${from.id}:${w.from.pin}`) || 0n;
    return v & mask(nd.type === "out" ? W(nd) : A.inW(nd, pin));
  };
  const set = (nd, pin, v, w) => { const k = `${nd.id}:${pin}`, x = v & mask(w); if (out.get(k) !== x) { out.set(k, x); return true; } return false; };
  function evalNode(g) {
    const t = g.type, n = W(g), m = mask(n);
    const ins = (pins) => pins.map((p) => val(g, p));
    switch (t) {
      case "not": return set(g, "Y", ~val(g, "A"), n);
      case "buf": return set(g, "Y", val(g, "A"), n);
      case "mux": return set(g, "Y", val(g, "S") & 1n ? val(g, "B") : val(g, "A"), n);
      case "add": { const s = val(g, "A") + val(g, "B") + val(g, "CIN"); return set(g, "S", s, n) | set(g, "COUT", s >> BigInt(n), 1); }
      case "sub": { const d = val(g, "A") - val(g, "B"); return set(g, "Y", d < 0n ? d + (1n << BigInt(n)) : d, n) | set(g, "BOUT", d < 0n ? 1n : 0n, 1); }
      case "cmp": { const a = val(g, "A"), b = val(g, "B"); return set(g, "EQ", a === b ? 1n : 0n, 1) | set(g, "LT", a < b ? 1n : 0n, 1) | set(g, "GT", a > b ? 1n : 0n, 1); }
      case "dec": { const ow = A.outW(g, "Y"), a = val(g, "A"); return set(g, "Y", a < BigInt(ow) ? 1n << a : 0n, ow); }
      case "muxn": { const k = clampBits(g.k || 4, 2, 16), s = Number(val(g, "S")); return set(g, "Y", val(g, "D" + Math.min(s, k - 1)), n); }
      case "merge": { let v = 0n; pinsOf(g).ins.forEach((p, i) => { v |= (val(g, p) & 1n) << BigInt(i); }); return set(g, "Y", v, n); }
      case "split": { const a = val(g, "A"); let ch = false; pinsOf(g).outs.forEach((p, i) => { ch = set(g, p, a >> BigInt(i), 1) || ch; }); return ch; }
      default: {
        const v = ins(pinsOf(g).ins);
        let r = v[0];
        for (const x of v.slice(1)) r = t.endsWith("and") ? r & x : t.endsWith("xor") || t === "xnor" ? r ^ x : r | x;
        if (t === "nand" || t === "nor" || t === "xnor") r = ~r;
        return set(g, "Y", r & m, n);
      }
    }
  }
  function evalComb() {
    for (const nd of D.nodes) {
      if (nd.type === "in") set(nd, "Y", inputs.get(nd.id) || 0n, W(nd));
      else if (nd.type === "const0" || nd.type === "const1") set(nd, "Y", nd.type === "const1" ? 1n : 0n, 1);
      else if (nd.type === "const") set(nd, "Y", BigInt(Math.max(0, Math.floor(+nd.value || 0))), W(nd));
      else if (SEQ_TYPES.includes(nd.type)) set(nd, "Q", regs.get(nd.id) || 0n, W(nd));
    }
    sim.unstable = false;
    for (let pass = 0; pass < 50; pass++) {
      let ch = false;
      for (const g of order) ch = evalNode(g) || ch;
      if (!ch) return;
    }
    sim.unstable = true;   // a combinational loop that oscillates
  }
  // evaluate, then let registers react (edges, resets) until nothing changes
  function settle() {
    for (let it = 0; it < 32; it++) {
      evalComb();
      const next = [];
      for (const f of ffs) {
        const clk = val(f, "CLK") & 1n;
        const hasRst = f.type === "dffr" || ((f.type === "reg" || f.type === "cnt") && A.srcWire(f, "RST_N"));
        const rst = hasRst ? val(f, "RST_N") & 1n : 1n;
        if (!prevClk.has(f.id)) prevClk.set(f.id, clk);
        const q = regs.get(f.id) || 0n;
        if (rst === 0n) { if (q !== 0n) next.push([f, 0n]); }
        else if (prevClk.get(f.id) === 0n && clk === 1n) {
          const en = f.type === "reg" || f.type === "cnt" ? val(f, "EN") & 1n : 1n;
          if (en) next.push([f, (f.type === "cnt" ? q + 1n : val(f, "D")) & mask(W(f))]);
        }
        prevClk.set(f.id, clk);
      }
      if (!next.length) return;
      for (const [f, v] of next) regs.set(f.id, v);
    }
  }
  sim.update = (Dn) => { build(Dn); settle(); return sim; };
  sim.setInput = (id, v) => { inputs.set(id, BigInt(v) & mask(W(byId[id]))); settle(); };
  sim.input = (id) => inputs.get(id) || 0n;
  sim.value = (id, pin) => out.get(`${id}:${pin}`) || 0n;
  sim.reg = (id) => regs.get(id) || 0n;
  sim.pin = (nd, pin) => val(nd, pin);   // value arriving at an input pin (e.g. of an output port)
  // inputs that drive a CLK pin (clock candidates)
  sim.clocks = () => {
    const ids = new Set();
    for (const f of ffs) { const w = A.srcWire(f, "CLK"); if (w && byId[w.from.node] && byId[w.from.node].type === "in") ids.add(w.from.node); }
    return D.nodes.filter((n) => ids.has(n.id));
  };
  sim.clock = (id, n = 1) => {
    for (let i = 0; i < n; i++) { sim.setInput(id, 1n); sim.setInput(id, 0n); sim.cycle++; }
  };
  sim.reset = () => { regs.clear(); prevClk.clear(); sim.cycle = 0; settle(); };
  sim.width = (nd) => W(nd);
  return sim.update(D0);
}
const VVEditor = { createSim, isOptional, GATE2, BLOCK_TYPES, PORT_TYPES, SEQ_TYPES, AUTO, pinsOf, analyze, toVerilog, ident, defaultBits, clampBits };
if (typeof module !== "undefined" && module.exports) module.exports = VVEditor;
