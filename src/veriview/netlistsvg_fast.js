/* Runs netlistsvg with a faster, non-recursive version of its split/join search.
 *
 *   node netlistsvg_fast.js <path to netlistsvg's bin/netlistsvg.js> <netlistsvg arguments...>
 *
 * netlistsvg (1.0.x) finds bus splits and joins with `gather` in built/FlatModule.js: it shortens
 * the searched bit range by one bit per *recursive call* and scans every port string for each
 * try. Wide datapaths (cache lines, 64-bit operands) make that quadratic, and the recursion
 * overflows Node's stack ("Maximum call stack size exceeded"). This loader replaces `gather`
 * when FlatModule.js is loaded with a loop that keeps the exact same decisions (same splits,
 * same joins, same order, hence the same drawing) but finds candidate ports through an index
 * by bit. Nothing on disk is changed; if the source does not look as expected, the original
 * code runs.
 */
"use strict";
const Module = require("module");
const fs = require("fs");
const path = require("path");

const FAST_GATHER = `
// ---- VeriView: fast gather (see netlistsvg_fast.js) ----
var __vvIndex = typeof WeakMap !== "undefined" ? new WeakMap() : null;
function __vvTokens(s) { return s.slice(1, -1).split(","); }
// the first bit of a bit string ",b0,b1,...,": constant time (queries can be thousands of bits long)
function __vvFirst(s) { var e = s.indexOf(",", 1); return e < 0 ? s.slice(1) : s.slice(1, e); }
// index of an array of bit strings that only grows by push: exact lookups + first element containing a query
function __vvGrowIndex(arr) {
    var ix = __vvIndex.get(arr);
    if (ix) return ix;
    // exact: (first bit, length) -> strings; hashing whole (long) query strings would cost O(length) per try
    ix = { arr: arr, exact: new Map(), byTok: new Map(), n: 0 };
    ix.sync = function () {
        for (; ix.n < arr.length; ix.n++) {
            var s = arr[ix.n];
            var ek = __vvFirst(s) + ":" + s.length, el = ix.exact.get(ek);
            if (!el) ix.exact.set(ek, el = []);
            el.push(s);
            var toks = __vvTokens(s);
            for (var t = 0; t < toks.length; t++) {
                var l = ix.byTok.get(toks[t]);
                if (!l) ix.byTok.set(toks[t], l = []);
                if (l[l.length - 1] !== ix.n) l.push(ix.n);
            }
        }
    };
    ix.has = function (q) {
        ix.sync();
        var el = ix.exact.get(__vvFirst(q) + ":" + q.length);
        if (!el) return false;
        for (var k = 0; k < el.length; k++) if (el[k] === q) return true;
        return false;
    };
    ix.first = function (q) {
        ix.sync();
        var l = ix.byTok.get(__vvFirst(q));
        if (!l) return -1;
        for (var k = 0; k < l.length; k++) if (arr[l[k]].indexOf(q) > -1) return l[k];
        return -1;
    };
    __vvIndex.set(arr, ix);
    return ix;
}
// index of an array of bit strings that only shrinks by splice (first occurrence of a value)
function __vvShrinkIndex(arr) {
    var ix = __vvIndex.get(arr);
    if (ix) return ix;
    ix = { count: new Map(), byTok: new Map() };
    for (var i = 0; i < arr.length; i++) {
        var s = arr[i];
        var c = ix.count.get(s) || 0;
        ix.count.set(s, c + 1);
        if (c) continue;
        var toks = __vvTokens(s);
        for (var t = 0; t < toks.length; t++) {
            var l = ix.byTok.get(toks[t]);
            if (!l) ix.byTok.set(toks[t], l = []);
            if (l[l.length - 1] !== s) l.push(s);
        }
    }
    ix.remove = function (s) {
        var c = ix.count.get(s);
        if (!c) return;
        ix.count.set(s, c - 1);
        arr.splice(arr.indexOf(s), 1);
    };
    ix.any = function (q) {
        var l = ix.byTok.get(__vvFirst(q));
        if (!l) return false;
        for (var k = 0; k < l.length; k++) if (ix.count.get(l[k]) && l[k].indexOf(q) > -1) return true;
        return false;
    };
    __vvIndex.set(arr, ix);
    return ix;
}
function gather(inputs, outputs, toSolve, start, end, splits, joins) {
    var I = __vvGrowIndex(inputs);
    var O = outputs.length ? __vvShrinkIndex(outputs) : null;
    for (;;) {
        // remove myself from outputs list if present
        if (O) O.remove(toSolve);
        if (start >= toSolve.length || end - start < 2) return;
        var query = toSolve.slice(start, end);
        // are there are perfect matches?
        if (I.has(query)) {
            if (query !== toSolve) addToDefaultDict(joins, toSolve, getIndicesString(toSolve, query, start));
            start = end - 1; end = toSolve.length;
            continue;
        }
        var index = I.first(query);
        // are there any partial matches?
        if (index !== -1) {
            if (query !== toSolve) addToDefaultDict(joins, toSolve, getIndicesString(toSolve, query, start));
            addToDefaultDict(splits, inputs[index], getIndicesString(inputs[index], query, 0));
            inputs.push(query);
            start = end - 1; end = toSolve.length;
            continue;
        }
        // are there any output matches?
        if (O && O.any(query)) {
            if (query !== toSolve) addToDefaultDict(joins, toSolve, getIndicesString(toSolve, query, start));
            gather(inputs, [], query, 0, query.length, splits, joins);
            inputs.push(query);
            return;
        }
        end = start + query.slice(0, -1).lastIndexOf(",") + 1;
    }
}
`;

const bin = process.argv[2];
if (!bin) {
  process.stderr.write("usage: node netlistsvg_fast.js <netlistsvg.js> args...\n");
  process.exit(2);
}
const origJs = Module._extensions[".js"];
Module._extensions[".js"] = function (m, filename) {
  if (path.basename(filename) === "FlatModule.js" && typeof WeakMap !== "undefined") {
    const src = fs.readFileSync(filename, "utf8");
    const i = src.indexOf("function gather(");
    const j = src.indexOf("function removeDups(");
    if (i > 0 && j > i && src.includes("function addToDefaultDict(") && src.includes("function getIndicesString(")) {
      m._compile(src.slice(0, i) + FAST_GATHER + src.slice(j), filename);
      return;
    }
  }
  return origJs(m, filename);
};
// netlistsvg's bin only acts as the main module: call its exported main() with the same arguments
const args = process.argv.slice(3);
const opt = (name) => { const i = args.indexOf(name); return i >= 0 ? args.splice(i, 2)[1] : undefined; };
const out = opt("-o"), skin = opt("--skin"), layout = opt("--layout");
if (args.length !== 1) {
  process.stderr.write("usage: node netlistsvg_fast.js <netlistsvg.js> input.json [-o out.svg] [--skin skin.svg] [--layout elk.json]\n");
  process.exit(2);
}
require(bin).main(args[0], out, skin, layout);
