#!/usr/bin/env bash
# Backward-compatible wrapper: runs VeriView from this checkout without installing it.
# Prefer `pip install .` and the `veriview` command.
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m veriview "$@"
