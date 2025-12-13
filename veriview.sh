#!/bin/bash

# ==============================================================================
# Script Name: veriview.sh
# Version: 3.0 (Custom Output & Path Support)
# Description: Installs dependencies, synthesizes HDL, handles filelists (.f),
#              and generates schematics with custom output paths.
# ==============================================================================

# --- Configuration ---
APP_NAME="VeriView"
VERSION="3.0"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- Helper Functions ---
log_info() { echo -e "${GREEN}[INFO] $1${NC}"; }
log_warn() { echo -e "${YELLOW}[WARN] $1${NC}"; }
log_err()  { echo -e "${RED}[ERROR] $1${NC}"; }

show_usage() {
    echo -e "${BLUE}============================================================${NC}"
    echo -e "${BLUE}   $APP_NAME v$VERSION - Verilog/SystemVerilog Schematic Viewer   ${NC}"
    echo -e "${BLUE}============================================================${NC}"
    echo ""
    echo "Usage:"
    echo "  ./veriview.sh <input_file> [top_module] [options]"
    echo ""
    echo "Arguments:"
    echo "  <input_file>      Single .v/.sv file OR a .f file list."
    echo "  [top_module]      (Optional) Name of the top module."
    echo ""
    echo "Options:"
    echo "  -o, --output <path>  Specify output filename and path."
    echo "  -f, --flatten        Flatten hierarchy (expand sub-modules into the top level)."
    echo "  -h, --help           Show this help message."
    echo ""
    echo "Defaults:"
    echo "  If -o is not provided, the script saves the file as:"
    echo "  1. <top_module>.svg (if top module is given)"
    echo "  2. <input_filename>.svg (otherwise)"
    echo ""
}

# --- 0. Argument Parsing ---

INPUT_FILE=""
TOP_MODULE=""
CUSTOM_OUTPUT=""

# Parse arguments loop
while [[ $# -gt 0 ]]; do
  key="$1"
  case $key in
    -o|--output)
      CUSTOM_OUTPUT="$2"
      shift # past argument
      shift # past value
      ;;
    -h|--help)
      show_usage
      exit 0
      ;;
      -f|--flatten)
      FLATTEN_FLAG=1
      shift ;;
    *)
      # Positional arguments handling
      if [ -z "$INPUT_FILE" ]; then
        INPUT_FILE="$1"
      elif [ -z "$TOP_MODULE" ]; then
        TOP_MODULE="$1"
      fi
      shift # past argument
      ;;
  esac
done

if [ -z "$INPUT_FILE" ]; then
    log_err "No input file provided."
    echo "Try './veriview.sh --help' for usage info."
    exit 1
fi

# --- 1. System & Dependency Check ---

install_dependencies() {
    MISSING=""
    if ! command -v yosys &> /dev/null; then MISSING="yosys"; fi
    if ! command -v npm &> /dev/null; then MISSING="$MISSING nodejs npm"; fi

    if [ ! -z "$MISSING" ]; then
        log_warn "Missing packages: $MISSING. Installing..."
        if [ -f /etc/os-release ]; then . /etc/os-release; OS=$NAME; fi
        
        if command -v apt-get &> /dev/null; then sudo apt-get update && sudo apt-get install -y $MISSING
        elif command -v dnf &> /dev/null; then sudo dnf install -y $MISSING
        elif command -v pacman &> /dev/null; then sudo pacman -S --noconfirm $MISSING
        elif [[ "$OSTYPE" == "darwin"* ]]; then brew install yosys node
        else log_err "Manual installation required for: $MISSING"; exit 1; fi
    fi

    if ! command -v netlistsvg &> /dev/null; then
        log_warn "Installing netlistsvg..."
        if [ "$EUID" -ne 0 ]; then sudo npm install -g netlistsvg
        else npm install -g netlistsvg; fi
    fi
}

install_dependencies

# --- 2. Determine Output File Name & Path ---

# Determine Logic Name for temp JSON
BASENAME_INPUT=$(basename "$INPUT_FILE" | sed 's/\.[^.]*$//')

if [ ! -z "$CUSTOM_OUTPUT" ]; then
    SVG_FILE="$CUSTOM_OUTPUT"
else
    # Default behavior requested by user
    if [ ! -z "$TOP_MODULE" ]; then
        SVG_FILE="${TOP_MODULE}.svg"
    else
        SVG_FILE="${BASENAME_INPUT}.svg"
    fi
fi

# Create Output Directory if it doesn't exist
OUTPUT_DIR=$(dirname "$SVG_FILE")
if [ ! -d "$OUTPUT_DIR" ] && [ "$OUTPUT_DIR" != "." ]; then
    log_info "Creating directory: $OUTPUT_DIR"
    mkdir -p "$OUTPUT_DIR"
fi

# Temp JSON file (keep it in the same dir as output to avoid moving issues)
JSON_FILE="${OUTPUT_DIR}/temp_netlist.json"

# --- 3. Process Input Files (.f handling) ---

INPUT_DIR=$(dirname "$INPUT_FILE")
EXTENSION="${INPUT_FILE##*.}"
YOSYS_READ_COMMANDS=""

if [[ "$EXTENSION" == "f" ]]; then
    log_info "Detected File List (.f) in: $INPUT_DIR"
    
    while IFS= read -r line || [ -n "$line" ]; do
        [[ -z "$line" ]] && continue
        [[ "$line" =~ ^[[:space:]]*// ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        
        RAW_PATH=$(echo "$line" | xargs)
        
        # Resolve absolute vs relative paths
        if [[ "$RAW_PATH" = /* ]]; then FINAL_PATH="$RAW_PATH"; else FINAL_PATH="$INPUT_DIR/$RAW_PATH"; fi
        
        SUB_EXT="${FINAL_PATH##*.}"
        
        if [[ "$SUB_EXT" == "sv" ]]; then
            YOSYS_READ_COMMANDS+=" read_verilog -sv $FINAL_PATH;"
        else
            YOSYS_READ_COMMANDS+=" read_verilog $FINAL_PATH;"
        fi
    done < "$INPUT_FILE"

elif [[ "$EXTENSION" == "sv" ]]; then
    YOSYS_READ_COMMANDS="read_verilog -sv $INPUT_FILE;"
else
    YOSYS_READ_COMMANDS="read_verilog $INPUT_FILE;"
fi

if [ ! -z "$TOP_MODULE" ]; then
   HIERARCHY_CMD="hierarchy -top $TOP_MODULE"
else
   HIERARCHY_CMD="hierarchy -auto-top"
fi

if [ "$FLATTEN_FLAG" == "1" ]; then
    FLATTEN_CMD="flatten;"
else
    FLATTEN_CMD=""
fi  

# --- 4. Synthesis & Generation ---
FULL_YOSYS_CMD="$YOSYS_READ_COMMANDS $FLATTEN_CMD $HIERARCHY_CMD; proc; opt; write_json $JSON_FILE"


log_info "Running Synthesis..."
YOSYS_OUT=$(yosys -q -p "$FULL_YOSYS_CMD" 2>&1)
if [ $? -ne 0 ]; then
    log_err "Yosys Synthesis Failed!"
    echo "$YOSYS_OUT"
    exit 1
fi

log_info "Generating Schematic -> $SVG_FILE"
netlistsvg "$JSON_FILE" -o "$SVG_FILE"
rm "$JSON_FILE"

# --- 5. Post-Processing & Viewing ---

if [ -f "$SVG_FILE" ]; then
    # Add White Background
    sed -i 's|<svg[^>]*>|&<rect x="0" y="0" width="100%" height="100%" fill="white"/>|' "$SVG_FILE"
    
    log_info "Done! Schematic saved at: $SVG_FILE"
else
    log_err "Failed to create SVG."
    exit 1
fi