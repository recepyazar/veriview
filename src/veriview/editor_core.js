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
const VVEditor = { isOptional, GATE2, BLOCK_TYPES, PORT_TYPES, SEQ_TYPES, AUTO, pinsOf, analyze, toVerilog, ident, defaultBits, clampBits };
if (typeof module !== "undefined" && module.exports) module.exports = VVEditor;
