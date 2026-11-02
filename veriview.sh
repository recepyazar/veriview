#!/usr/bin/env bash
# ==============================================================================
# VeriView - Verilog/SystemVerilog -> SVG schematic generator
#
# Wraps Yosys (+ optional sv2v / yosys-slang SystemVerilog frontends) and
# netlistsvg: reads HDL sources or .f file lists, synthesizes a netlist and
# renders it as an SVG schematic.
# ==============================================================================

set -o pipefail

APP_NAME="VeriView"
VERSION="4.0.0"
SV2V_VERSION="v0.0.13"

# --- Logging ------------------------------------------------------------------
# Colors only when stderr is a terminal and NO_COLOR is not set.
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
fi

QUIET=0
log_info() { [[ $QUIET -eq 1 ]] || printf '%s[INFO] %s%s\n' "$GREEN" "$1" "$NC" >&2; }
log_warn() { printf '%s[WARN] %s%s\n' "$YELLOW" "$1" "$NC" >&2; }
log_err()  { printf '%s[ERROR] %s%s\n' "$RED" "$1" "$NC" >&2; }
die()      { log_err "$1"; exit "${2:-1}"; }

show_usage() {
    cat <<EOF
${BLUE}$APP_NAME v$VERSION - Verilog/SystemVerilog schematic generator${NC}

Usage:
  veriview.sh [options] <input>... [top_module]

Inputs:
  <input>                 One or more .v / .sv / .svh files and/or .f file lists.
  [top_module]            (Optional, legacy form) top module name; same as -t.

Options:
  -t, --top <name>        Top module (default: auto-detected).
  -o, --output <path>     Output SVG path. With --split: output directory.
  -f, -F <file.f>         Read a file list (same as passing the .f as input).
  -I, --incdir <dir>      Add an include directory (repeatable).
  -D, --define <N[=V]>    Add a preprocessor define (repeatable).
      --flatten           Flatten hierarchy (expand sub-modules).
      --split             Render every module to its own SVG.
      --frontend <name>   auto | yosys | sv2v | slang   (default: auto)
                          auto: Yosys' built-in parser, falling back to
                          yosys-slang or sv2v for unsupported SystemVerilog
                          (e.g. unpacked array ports).
      --skin <file.svg>   Custom netlistsvg skin.
      --bg <color|none>   Background color injected into the SVG (default: white).
      --keep-json         Keep the Yosys JSON netlist next to the SVG.
      --open              Open the result (xdg-open / open / Windows on WSL).
      --install-deps      Install missing dependencies (yosys, node, netlistsvg, sv2v).
  -y, --yes               Do not ask for confirmation when installing.
  -q, --quiet             Only print warnings and errors.
  -V, --version           Print version.
  -h, --help              Show this help.

File list (.f) syntax:
  one source per line, // and # comments, +incdir+<dir>[+<dir>...],
  +define+<N>[=<V>][+...], -f/-F <nested.f>, -v <lib file>, -I<dir>, -D<N>,
  \$VAR / \${VAR} / \$(VAR) environment variables.
  Relative paths are resolved against the directory of the .f file.

Examples:
  veriview.sh counter.sv
  veriview.sh design.f -t cpu_core -o build/cpu.svg
  veriview.sh rtl/*.sv -I rtl/include -D SYNTH --flatten --open
EOF
}

# --- Argument parsing ---------------------------------------------------------
INPUTS=()
INCDIRS=()
DEFINES=()
TOP_MODULE=""
CUSTOM_OUTPUT=""
FLATTEN=0
SPLIT=0
FRONTEND="auto"
SKIN=""
BG_COLOR="white"
KEEP_JSON=0
OPEN_RESULT=0
INSTALL_DEPS=0
ASSUME_YES=0

need_value() { [[ -n "${2:-}" && "${2:0:1}" != "-" ]] || die "Option '$1' requires a value."; }

is_hdl_or_list() { [[ "$1" =~ \.(v|sv|svh|vh|f)$ ]]; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)    need_value "$1" "${2:-}"; CUSTOM_OUTPUT="$2"; shift 2 ;;
        -t|--top)       need_value "$1" "${2:-}"; TOP_MODULE="$2"; shift 2 ;;
        -f|-F)          need_value "$1" "${2:-}"; INPUTS+=("$2"); shift 2 ;;
        -I|--incdir)    need_value "$1" "${2:-}"; INCDIRS+=("$2"); shift 2 ;;
        -I*)            INCDIRS+=("${1#-I}"); shift ;;
        -D|--define)    need_value "$1" "${2:-}"; DEFINES+=("$2"); shift 2 ;;
        -D*)            DEFINES+=("${1#-D}"); shift ;;
        --flatten)      FLATTEN=1; shift ;;
        --split)        SPLIT=1; shift ;;
        --frontend)     need_value "$1" "${2:-}"; FRONTEND="$2"; shift 2 ;;
        --skin)         need_value "$1" "${2:-}"; SKIN="$2"; shift 2 ;;
        --bg)           need_value "$1" "${2:-}"; BG_COLOR="$2"; shift 2 ;;
        --keep-json)    KEEP_JSON=1; shift ;;
        --open)         OPEN_RESULT=1; shift ;;
        --install-deps) INSTALL_DEPS=1; shift ;;
        -y|--yes)       ASSUME_YES=1; shift ;;
        -q|--quiet)     QUIET=1; shift ;;
        -V|--version)   echo "$APP_NAME $VERSION"; exit 0 ;;
        -h|--help)      show_usage; exit 0 ;;
        --)             shift; INPUTS+=("$@"); break ;;
        -*)             die "Unknown option: $1 (see --help)" ;;
        *)
            if [[ -e "$1" ]] || is_hdl_or_list "$1"; then
                INPUTS+=("$1")
            elif [[ -z "$TOP_MODULE" ]]; then
                TOP_MODULE="$1"
            else
                die "Unexpected argument: '$1' (top module already set to '$TOP_MODULE')."
            fi
            shift ;;
    esac
done

[[ -z "$TOP_MODULE" || "$TOP_MODULE" =~ ^[A-Za-z_][A-Za-z0-9_\$]*$ ]] || die "Invalid top module name: '$TOP_MODULE'"
case "$FRONTEND" in auto|yosys|sv2v|slang) ;; *) die "Invalid --frontend '$FRONTEND' (auto|yosys|sv2v|slang)." ;; esac
[[ -z "$SKIN" || -f "$SKIN" ]] || die "Skin file not found: $SKIN"

# --- Dependencies -------------------------------------------------------------
run_privileged() {
    if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    [[ -t 0 ]] || return 1
    local reply
    read -r -p "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy] ]]
}

install_system_packages() {
    local need_yosys=$1 need_node=$2
    if [[ "$OSTYPE" == darwin* ]]; then
        command -v brew >/dev/null || die "Homebrew is required on macOS: https://brew.sh"
        [[ $need_yosys -eq 1 ]] && brew install yosys
        [[ $need_node  -eq 1 ]] && brew install node
        return 0
    fi
    local pkgs=()
    [[ $need_yosys -eq 1 ]] && pkgs+=(yosys)
    [[ $need_node  -eq 1 ]] && pkgs+=(nodejs npm)
    if command -v apt-get >/dev/null; then
        run_privileged apt-get update && run_privileged apt-get install -y "${pkgs[@]}"
    elif command -v dnf >/dev/null; then
        run_privileged dnf install -y "${pkgs[@]}"
    elif command -v pacman >/dev/null; then
        run_privileged pacman -S --needed --noconfirm "${pkgs[@]}"
    else
        die "Unsupported package manager. Please install manually: ${pkgs[*]}"
    fi
}

install_netlistsvg() {
    local prefix
    prefix=$(npm prefix -g 2>/dev/null)
    if [[ -n "$prefix" && -w "$prefix/lib" ]] || [[ -n "$prefix" && -w "$prefix" ]]; then
        npm install -g netlistsvg
    else
        run_privileged npm install -g netlistsvg
    fi
}

install_sv2v() {
    local os asset dest tmp
    case "$OSTYPE" in
        linux*)  os="Linux" ;;
        darwin*) os="macOS" ;;
        *) die "No prebuilt sv2v for '$OSTYPE'. See https://github.com/zachjs/sv2v" ;;
    esac
    command -v curl  >/dev/null || die "curl is required to download sv2v."
    command -v unzip >/dev/null || die "unzip is required to install sv2v."
    asset="https://github.com/zachjs/sv2v/releases/download/${SV2V_VERSION}/sv2v-${os}.zip"
    dest="${HOME}/.local/bin"
    tmp=$(mktemp -d)
    log_info "Downloading sv2v ${SV2V_VERSION} -> $dest/sv2v"
    if curl -fsSL "$asset" -o "$tmp/sv2v.zip" && unzip -q "$tmp/sv2v.zip" -d "$tmp"; then
        mkdir -p "$dest"
        install -m 755 "$tmp/sv2v-${os}/sv2v" "$dest/sv2v"
        rm -rf "$tmp"
        case ":$PATH:" in *":$dest:"*) ;; *) log_warn "$dest is not in PATH; add it to use sv2v." ; export PATH="$dest:$PATH" ;; esac
    else
        rm -rf "$tmp"
        die "Failed to download sv2v from $asset"
    fi
}

check_dependencies() {
    local need_yosys=0 need_node=0 need_nsvg=0 need_sv2v=0
    command -v yosys      >/dev/null || need_yosys=1
    command -v npm        >/dev/null || need_node=1
    command -v netlistsvg >/dev/null || need_nsvg=1
    [[ $INSTALL_DEPS -eq 1 ]] && ! command -v sv2v >/dev/null && need_sv2v=1
    [[ "$FRONTEND" == "sv2v" ]] && ! command -v sv2v >/dev/null && need_sv2v=1
    # npm is only needed to install netlistsvg.
    [[ $need_nsvg -eq 0 ]] && need_node=0

    if (( need_yosys + need_nsvg + need_sv2v == 0 )); then return 0; fi

    local missing=()
    [[ $need_yosys -eq 1 ]] && missing+=(yosys)
    [[ $need_nsvg  -eq 1 ]] && missing+=(netlistsvg)
    [[ $need_sv2v  -eq 1 ]] && missing+=(sv2v)

    if [[ $INSTALL_DEPS -eq 1 ]] || confirm "Missing: ${missing[*]}. Install now?"; then
        if (( need_yosys + need_node > 0 )); then install_system_packages "$need_yosys" "$need_node" || die "Package installation failed."; fi
        [[ $need_nsvg -eq 1 ]] && { install_netlistsvg || die "netlistsvg installation failed."; }
        [[ $need_sv2v -eq 1 ]] && install_sv2v
    else
        die "Missing dependencies: ${missing[*]}. Re-run with --install-deps to install them."
    fi
}

check_dependencies
if [[ ${#INPUTS[@]} -eq 0 ]]; then
    [[ $INSTALL_DEPS -eq 1 ]] && { log_info "Dependencies are installed."; exit 0; }
    die "No input file provided. Try '$0 --help'."
fi

# --- Input collection (.f parsing) --------------------------------------------
SOURCES=()

expand_env() {
    # Expands $VAR, ${VAR} and $(VAR). Unset variables expand to "".
    local s="$1" out="" name
    while [[ "$s" =~ ^([^\$]*)\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|\(([A-Za-z_][A-Za-z0-9_]*)\)|([A-Za-z_][A-Za-z0-9_]*))(.*)$ ]]; do
        name="${BASH_REMATCH[3]}${BASH_REMATCH[4]}${BASH_REMATCH[5]}"
        out+="${BASH_REMATCH[1]}${!name:-}"
        s="${BASH_REMATCH[6]}"
    done
    printf '%s' "$out$s"
}

resolve_path() { # <base_dir> <path>
    if [[ "$2" == /* ]]; then printf '%s' "$2"; else printf '%s' "$1/$2"; fi
}

declare -A SEEN_LISTS=()

parse_filelist() {
    local list="$1" dir line tok i
    [[ -f "$list" ]] || die "File list not found: $list"
    local key
    key=$(cd "$(dirname "$list")" && pwd)/$(basename "$list")
    [[ -n "${SEEN_LISTS[$key]:-}" ]] && die "Recursive file list include: $list"
    SEEN_LISTS[$key]=1
    dir=$(dirname "$list")
    log_info "Reading file list: $list"

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%//*}"                          # strip // comments
        [[ "$line" =~ ^[[:space:]]*# ]] && continue  # skip # comment lines
        line="${line%$'\r'}"
        line=$(expand_env "$line")
        # trim
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue

        # Plain source line: the whole line is one path (allows spaces).
        if [[ "${line:0:1}" != "+" && "${line:0:1}" != "-" ]]; then
            SOURCES+=("$(resolve_path "$dir" "$line")")
            continue
        fi

        local -a toks
        read -r -a toks <<< "$line"
        for (( i = 0; i < ${#toks[@]}; i++ )); do
            tok="${toks[$i]}"
            case "$tok" in
                +incdir+*)
                    local IFS_SAVE="$IFS" d; IFS='+'
                    for d in ${tok#+incdir+}; do [[ -n "$d" ]] && INCDIRS+=("$(resolve_path "$dir" "$d")"); done
                    IFS="$IFS_SAVE" ;;
                +define+*)
                    local IFS_SAVE="$IFS" d; IFS='+'
                    for d in ${tok#+define+}; do [[ -n "$d" ]] && DEFINES+=("$d"); done
                    IFS="$IFS_SAVE" ;;
                -f|-F)  (( i++ )); parse_filelist "$(resolve_path "$dir" "${toks[$i]}")" ;;
                -v)     (( i++ )); SOURCES+=("$(resolve_path "$dir" "${toks[$i]}")") ;;
                -I)     (( i++ )); INCDIRS+=("$(resolve_path "$dir" "${toks[$i]}")") ;;
                -I*)    INCDIRS+=("$(resolve_path "$dir" "${tok#-I}")") ;;
                -D)     (( i++ )); DEFINES+=("${toks[$i]}") ;;
                -D*)    DEFINES+=("${tok#-D}") ;;
                -y|+libext+*|-sv|-timescale*|+*) log_warn "Ignoring unsupported file list option: $tok"
                        [[ "$tok" == "-y" ]] && (( i++ )) ;;
                *)      SOURCES+=("$(resolve_path "$dir" "$tok")") ;;
            esac
        done
    done < "$list"
}

for input in "${INPUTS[@]}"; do
    if [[ "$input" == *.f ]]; then
        parse_filelist "$input"
    else
        SOURCES+=("$input")
    fi
done

[[ ${#SOURCES[@]} -gt 0 ]] || die "No source files found."
for src in "${SOURCES[@]}"; do
    [[ -f "$src" ]] || die "Source file not found: $src"
done

for d in "${INCDIRS[@]}"; do [[ -d "$d" ]] || log_warn "Include directory not found: $d"; done
for d in "${DEFINES[@]}"; do
    [[ "$d" =~ ^[A-Za-z_][A-Za-z0-9_]*(=[^[:space:]\"]*)?$ ]] || die "Invalid define (no spaces/quotes allowed): '$d'"
done

HAS_SV=0
for src in "${SOURCES[@]}"; do [[ "$src" =~ \.(sv|svh)$ ]] && HAS_SV=1; done

# --- Workspace ----------------------------------------------------------------
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/veriview.XXXXXX") || die "Cannot create temp dir."
trap 'rm -rf "$WORK_DIR"' EXIT

# --- Yosys script generation --------------------------------------------------
yq() { # quote a string for a Yosys script
    local s="${1//\\/\\\\}"
    printf '"%s"' "${s//\"/\\\"}"
}

slang_available() { yosys -q -p "plugin -i slang" >/dev/null 2>&1; }

SV2V_OUT="$WORK_DIR/sv2v_out.v"
SV2V_DONE=0
run_sv2v() {
    [[ $SV2V_DONE -eq 1 ]] && return 0
    command -v sv2v >/dev/null || { echo "sv2v is not installed (run with --install-deps)"; return 1; }
    local args=() d
    for d in "${INCDIRS[@]}"; do args+=("-I" "$d"); done
    for d in "${DEFINES[@]}"; do args+=("-D" "$d"); done
    sv2v "${args[@]}" "${SOURCES[@]}" > "$SV2V_OUT.raw" 2>"$WORK_DIR/sv2v.err" || { cat "$WORK_DIR/sv2v.err"; return 1; }
    # sv2v lowers elaboration-time $error/$warning to "initial $display(...)",
    # which Yosys ignores. Turn them back into $error/$warning so parameter
    # checks still stop synthesis.
    sed -E \
        -e 's/initial \$display\("Error \[elaboration\][^"]*", (.*)\);$/$error(\1);/' \
        -e 's/initial \$display\("Warning \[elaboration\][^"]*", (.*)\);$/$warning(\1);/' \
        "$SV2V_OUT.raw" > "$SV2V_OUT"
    SV2V_DONE=1
}

# Emit the read commands for a given frontend.
emit_read() {
    local fe="$1" top="$2" opts="" d src i=0
    # Yosys does not unquote option values, so include dirs are passed via
    # space-free symlinks inside the work dir.
    for d in "${INCDIRS[@]}"; do
        [[ -e "$WORK_DIR/inc$i" ]] || ln -s "$(cd "$d" 2>/dev/null && pwd || echo "$d")" "$WORK_DIR/inc$i"
        opts+=" -I $WORK_DIR/inc$i"; (( i++ ))
    done
    for d in "${DEFINES[@]}"; do opts+=" -D $d"; done
    case "$fe" in
        yosys)
            for src in "${SOURCES[@]}"; do
                if [[ "$src" =~ \.(sv|svh)$ ]]; then
                    echo "read_verilog -sv$opts $(yq "$src")"
                else
                    echo "read_verilog$opts $(yq "$src")"
                fi
            done ;;
        sv2v)
            echo "read_verilog $(yq "$SV2V_OUT")" ;;
        slang)
            echo "plugin -i slang"
            local files=""
            for src in "${SOURCES[@]}"; do files+=" $(yq "$src")"; done
            [[ -n "$top" ]] && opts+=" --top $top"
            echo "read_slang$opts$files" ;;
    esac
}

# synthesize <frontend> <top|""> <json_out>; prints Yosys errors on failure.
synthesize() {
    local fe="$1" top="$2" json="$3" ys="$WORK_DIR/synth.ys"
    if [[ "$fe" == "sv2v" ]]; then run_sv2v || return 1; fi
    {
        emit_read "$fe" "$top"
        if [[ -n "$top" ]]; then echo "hierarchy -check -top $top"; else echo "hierarchy -check -auto-top"; fi
        echo "proc"
        [[ $FLATTEN -eq 1 ]] && echo "flatten"
        echo "opt"
        echo "opt_clean -purge"
        echo "write_json $(yq "$json")"
    } > "$ys"
    yosys -q -s "$ys" 2>&1
}

# list_modules <frontend>: writes user module names, one per line, to $MODULES_FILE.
MODULES_FILE="$WORK_DIR/module_names.txt"
list_modules() {
    local fe="$1" ys="$WORK_DIR/list.ys" out="$WORK_DIR/modules.txt"
    if [[ "$fe" == "sv2v" ]]; then run_sv2v || return 1; fi
    { emit_read "$fe" ""; echo "tee -q -o $(yq "$out") ls"; } > "$ys"
    yosys -q -s "$ys" 2>&1 >/dev/null || return 1
    sed -n 's/^  \(\$abstract\\\)\{0,1\}\([^ ]*\)$/\2/p' "$out" | sort -u > "$MODULES_FILE"
}

candidate_frontends() {
    case "$FRONTEND" in
        auto)
            echo yosys
            if [[ $HAS_SV -eq 1 ]]; then
                slang_available && echo slang
                command -v sv2v >/dev/null && echo sv2v
            fi ;;
        *) echo "$FRONTEND" ;;
    esac
}

# Try each candidate frontend in order; sets SELECTED_FE on success.
SELECTED_FE=""
LAST_ERROR=""
with_frontend() { # <command> [args...]  (frontend is passed as first arg to command)
    local fe out
    for fe in $(candidate_frontends); do
        if out=$("$1" "$fe" "${@:2}"); then
            SELECTED_FE="$fe"
            [[ -n "$out" ]] && printf '%s\n' "$out"
            return 0
        fi
        LAST_ERROR="$out"
        log_warn "Frontend '$fe' failed${out:+: $(head -n1 <<< "$out")}"
    done
    return 1
}

fail_synthesis() {
    log_err "Synthesis failed."
    [[ -n "$LAST_ERROR" ]] && printf '%s\n' "$LAST_ERROR" >&2
    if [[ $HAS_SV -eq 1 && "$FRONTEND" == "auto" && "$LAST_ERROR" == *"syntax error"* ]] && ! command -v sv2v >/dev/null && ! slang_available; then
        log_warn "Yosys' built-in parser supports only part of SystemVerilog (e.g. no unpacked array ports)."
        log_warn "Install sv2v for full support: $0 --install-deps"
    fi
    exit 1
}

# --- Rendering ----------------------------------------------------------------
add_background() {
    local svg="$1"
    [[ "$BG_COLOR" == "none" ]] && return 0
    awk -v bg="$BG_COLOR" '
        !done && match($0, /<svg[^>]*>/) {
            $0 = substr($0, 1, RSTART + RLENGTH - 1) "<rect x=\"0\" y=\"0\" width=\"100%\" height=\"100%\" fill=\"" bg "\"/>" substr($0, RSTART + RLENGTH)
            done = 1
        }
        { print }' "$svg" > "$svg.tmp" && mv "$svg.tmp" "$svg"
}

render() { # <json> <svg>
    local json="$1" svg="$2" args=() out
    [[ -n "$SKIN" ]] && args+=(--skin "$SKIN")
    mkdir -p "$(dirname "$svg")" || die "Cannot create directory for $svg"
    if ! out=$(netlistsvg "$json" -o "$svg" "${args[@]}" 2>&1) || [[ ! -s "$svg" ]]; then
        log_err "netlistsvg failed for $svg"
        printf '%s\n' "$out" >&2
        return 1
    fi
    add_background "$svg"
    if [[ $KEEP_JSON -eq 1 ]]; then cp "$json" "${svg%.svg}.json"; fi
    log_info "Schematic saved: $svg"
}

open_result() {
    local f="$1"
    if grep -qi microsoft /proc/version 2>/dev/null && command -v wslpath >/dev/null; then
        explorer.exe "$(wslpath -w "$f")" >/dev/null 2>&1 || true   # explorer.exe returns 1 even on success
    elif [[ "$OSTYPE" == darwin* ]]; then
        open "$f"
    elif command -v xdg-open >/dev/null; then
        xdg-open "$f" >/dev/null 2>&1 &
    else
        log_warn "Don't know how to open files on this system."
    fi
}

# --- Main ---------------------------------------------------------------------
first_base=$(basename "${INPUTS[0]}"); first_base="${first_base%.*}"

if [[ $SPLIT -eq 1 ]]; then
    OUT_DIR="${CUSTOM_OUTPUT:-.}"
    log_info "Listing modules..."
    with_frontend list_modules || fail_synthesis
    [[ -s "$MODULES_FILE" ]] || die "No modules found."
    log_info "Frontend: $SELECTED_FE"
    status=0
    while IFS= read -r m; do
        json="$WORK_DIR/$m.json"
        if out=$(synthesize "$SELECTED_FE" "$m" "$json"); then
            render "$json" "$OUT_DIR/$m.svg" || status=1
        else
            log_err "Synthesis of module '$m' failed:"; printf '%s\n' "$out" >&2; status=1
        fi
    done < "$MODULES_FILE"
    [[ $OPEN_RESULT -eq 1 ]] && open_result "$OUT_DIR"
    exit $status
fi

if [[ -n "$CUSTOM_OUTPUT" ]]; then
    SVG_FILE="$CUSTOM_OUTPUT"
    [[ "$SVG_FILE" == */ || -d "$SVG_FILE" ]] && SVG_FILE="${SVG_FILE%/}/${TOP_MODULE:-$first_base}.svg"
elif [[ -n "$TOP_MODULE" ]]; then
    SVG_FILE="${TOP_MODULE}.svg"
else
    SVG_FILE="${first_base}.svg"
fi

JSON_FILE="$WORK_DIR/netlist.json"
log_info "Running synthesis..."
with_frontend synthesize "$TOP_MODULE" "$JSON_FILE" >/dev/null || fail_synthesis
[[ "$SELECTED_FE" != "yosys" || "$FRONTEND" != "auto" ]] && log_info "Frontend: $SELECTED_FE"

log_info "Generating schematic -> $SVG_FILE"
render "$JSON_FILE" "$SVG_FILE" || exit 1
[[ $OPEN_RESULT -eq 1 ]] && open_result "$SVG_FILE"
exit 0
