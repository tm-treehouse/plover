#!/usr/bin/env bash
# =============================================================================
# build.sh — synthesis driver stub
#
# Placeholder for the real synthesis flow. Once a vendor is chosen, replace
# the body below with the actual tool invocations (or, more idiomatically,
# add a default_tool to the `syn` target in plover/plover.core and let
# FuseSoC drive Edalize). This script exists so the directory pattern is in
# place; nothing here is operational yet.
# =============================================================================
set -euo pipefail

cat <<'EOF'
plover/syn/scripts/build.sh — synthesis flow is not yet implemented.

To wire up a real flow:

  1. Pick a vendor (Vivado / Quartus / yosys+nextpnr / ...).
  2. Add `default_tool: <vendor>` and the matching `tools:` block to the
     `syn` target in plover/plover.core. Add the constraints file
     (plover/syn/constraints/plover.sdc) to a `constraints` fileset.
  3. Run:
         fusesoc run --target=syn plover
     Edalize handles vendor-specific invocation from there.

Until then this script is a no-op.
EOF
