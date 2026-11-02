#!/usr/bin/env bash
# VeriView test suite: runs the example designs and CLI edge cases.
# Usage: tests/run_tests.sh
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VV="$ROOT/veriview.sh"
EX="$ROOT/ex_design"
OUT=$(mktemp -d "${TMPDIR:-/tmp}/veriview-test.XXXXXX")
trap 'rm -rf "$OUT"' EXIT
export NO_COLOR=1

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS + 1)); echo "  ok    $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL  $1"; [[ -n "${2:-}" ]] && sed 's/^/        /' <<< "$2"; }
skip() { SKIP=$((SKIP + 1)); echo "  skip  $1 ($2)"; }

is_svg() { [[ -s "$1" ]] && grep -q '<svg' "$1"; }

# expect_svg <name> <svg> <veriview args...>
expect_svg() {
    local name="$1" svg="$2" log; shift 2
    if log=$("$VV" -q "$@" 2>&1) && is_svg "$svg"; then ok "$name"; else bad "$name" "$log"; fi
}

# expect_fail <name> <message regex> <veriview args...>
expect_fail() {
    local name="$1" re="$2" log; shift 2
    if log=$("$VV" -q "$@" 2>&1); then bad "$name (expected failure)" "$log"
    elif [[ ! "$log" =~ $re ]]; then bad "$name (unexpected message)" "$log"
    else ok "$name"; fi
}

has_sv_frontend() { command -v sv2v >/dev/null || yosys -q -p "plugin -i slang" >/dev/null 2>&1; }

echo "== Example designs"
expect_svg "counter.sv"            "$OUT/counter.svg"  "$EX/counter.sv" -o "$OUT/counter.svg"
expect_svg "seqdect.sv"            "$OUT/seqdect.svg"  "$EX/seqdect.sv" -o "$OUT/seqdect.svg"
expect_svg "mux_basic.sv"          "$OUT/mux_basic.svg" "$EX/mux/mux_basic.sv" -o "$OUT/mux_basic.svg"
expect_svg "rca.f"                 "$OUT/rca.svg"      "$EX/rca/rca.f" -o "$OUT/rca.svg"
expect_svg "rca.f legacy top"      "$OUT/fa.svg"       "$EX/rca/rca.f" fa -o "$OUT/fa.svg"
expect_svg "rca.f -t fa"           "$OUT/fa2.svg"      "$EX/rca/rca.f" -t fa -o "$OUT/fa2.svg"
expect_svg "rca.f --flatten"       "$OUT/rca_flat.svg" "$EX/rca/rca.f" --flatten -o "$OUT/rca_flat.svg"
expect_svg "gray.f --flatten"      "$OUT/b2g2b.svg"    "$EX/gray/gray.f" --flatten -o "$OUT/b2g2b.svg"
expect_svg "-f gray.f"             "$OUT/gray.svg"     -f "$EX/gray/gray.f" -t b2g2b -o "$OUT/gray.svg"
expect_svg "multiple inputs"       "$OUT/multi.svg"    "$EX/rca/ha.sv" "$EX/rca/fa.sv" -o "$OUT/multi.svg"

if has_sv_frontend; then
    expect_svg "mux.sv (unpacked array ports)" "$OUT/mux.svg" "$EX/mux/mux.sv" -o "$OUT/mux.svg"
    expect_svg "shift_reg.sv (unpacked array output)" "$OUT/shift_reg.svg" "$EX/shift_reg.sv" -o "$OUT/shift_reg.svg"
    sed 's/NUM_REGS   = 2/NUM_REGS   = 0/' "$EX/shift_reg.sv" > "$OUT/shift_reg0.sv"
    expect_fail "shift_reg NUM_REGS=0 check" "NUM_REGS must be" "$OUT/shift_reg0.sv" -o "$OUT/sr0.svg"
else
    skip "mux.sv (unpacked array ports)" "needs sv2v or yosys-slang"
    skip "shift_reg.sv (unpacked array output)" "needs sv2v or yosys-slang"
fi

echo "== Options"
log=$("$VV" -q "$EX/rca/rca.f" --split -o "$OUT/split" 2>&1)
if is_svg "$OUT/split/rca.svg" && is_svg "$OUT/split/fa.svg" && is_svg "$OUT/split/ha.svg"; then ok "--split"; else bad "--split" "$log"; fi

"$VV" -q "$EX/counter.sv" -o "$OUT/keep.svg" --keep-json >/dev/null 2>&1
[[ -s "$OUT/keep.json" ]] && ok "--keep-json" || bad "--keep-json"

"$VV" -q "$EX/counter.sv" -o "$OUT/bg.svg" --bg '#123456' >/dev/null 2>&1
grep -q 'fill="#123456"' "$OUT/bg.svg" && ok "--bg color" || bad "--bg color"

"$VV" -q "$EX/counter.sv" -o "$OUT/nobg.svg" --bg none >/dev/null 2>&1
is_svg "$OUT/nobg.svg" && ! grep -q 'width="100%" height="100%"' "$OUT/nobg.svg" && ok "--bg none" || bad "--bg none"

expect_svg "output directory" "$OUT/dir/counter.svg" "$EX/counter.sv" -o "$OUT/dir/"

mkdir -p "$OUT/with space"
cp "$EX/counter.sv" "$OUT/with space/"
expect_svg "path with spaces" "$OUT/with space/c.svg" "$OUT/with space/counter.sv" -o "$OUT/with space/c.svg"

echo "== File lists"
mkdir -p "$OUT/fl/inc dir" "$OUT/fl/sub"
echo '`define W 3' > "$OUT/fl/inc dir/defs.svh"
cat > "$OUT/fl/sub/top.sv" <<'EOF'
`include "defs.svh"
module top(input logic [`W-1:0] a, output logic [`W-1:0] y);
`ifdef INVERT
  assign y = ~a;
`else
  assign y = a;
`endif
endmodule
EOF
printf '$(SUBDIR)/top.sv  // trailing comment\n' > "$OUT/fl/sub/inner.f"
printf '# comment\n+define+INVERT\n-f ${NEST}/inner.f\n' > "$OUT/fl/main.f"
if SUBDIR=. NEST=sub "$VV" -q "$OUT/fl/main.f" -I "$OUT/fl/inc dir" -o "$OUT/fl/out.svg" --keep-json >/dev/null 2>&1 \
   && grep -q '"\$not"' "$OUT/fl/out.json"; then ok "+define, nested -f, env vars, -I with spaces"; else bad "+define, nested -f, env vars, -I with spaces"; fi
"$VV" -q "$OUT/fl/sub/top.sv" -I "$OUT/fl/inc dir" -o "$OUT/fl/plain.svg" --keep-json >/dev/null 2>&1
! grep -q '"\$not"' "$OUT/fl/plain.json" && ok "no define -> no inverter" || bad "no define -> no inverter"

printf -- '-f self.f\n' > "$OUT/fl/self.f"
expect_fail "recursive file list" "Recursive"            "$OUT/fl/self.f"

echo "== Errors"
expect_fail "missing input"       "No input file"         -o "$OUT/x.svg"
expect_fail "missing file"        "not found"             "$EX/nope.sv"
expect_fail "unknown option"      "Unknown option"        "$EX/counter.sv" --bogus
expect_fail "-o without value"    "requires a value"      "$EX/counter.sv" -o
expect_fail "unknown top"         "not found"             "$EX/counter.sv" -t nosuch -o "$OUT/x.svg"
expect_fail "invalid top name"    "Invalid top"           "$EX/counter.sv" -t 'a;b'
expect_fail "invalid frontend"    "Invalid --frontend"    "$EX/counter.sv" --frontend foo
expect_fail "invalid define"      "Invalid define"        "$EX/counter.sv" -D 'A B'
printf 'module broken(input a, output y);\n  assign y = ;\nendmodule\n' > "$OUT/broken.v"
expect_fail "syntax error"        "syntax error"          "$OUT/broken.v"
[[ ! -e "$OUT/x.svg" ]] && ok "no output on failure" || bad "no output on failure"

echo
echo "passed: $PASS  failed: $FAIL  skipped: $SKIP"
[[ $FAIL -eq 0 ]]
