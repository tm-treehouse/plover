"""FuseSoC + pytest harness for the plover project top.

Tiny shim over the shared harness in ``tools/dv_harness.py``. The top
needs two knobs the simpler units don't:

* live_dir_map={"plover": ROOT/"top"} — the project-top core's RTL lives
  at top/rtl/plover.sv, not units/plover/rtl/plover.sv.
* c_include_env="PLOVER_RDL_INCLUDE_DIRS" — the firmware bridge reads
  this to find the peakrdl-cheader generated headers it needs to
  compile against.

VERSION overrides are passed as build parameters to plover.sv, which
forwards them to its syscon instance.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent  # top/dv
PROJ_DIR = HERE.parent                  # top/
ROOT = PROJ_DIR.parent                  # repo root
sys.path.insert(0, str(ROOT))

from tools.dv_harness import HarnessConfig, run_testcase  # noqa: E402


# Must match EXPECTED_SYSCON_VERSION in plover_test.py.
VERSION_OVERRIDE      = 0xCAFE_F00D
VERSION_HASH_OVERRIDE = 0x0000_0001

CFG = HarnessConfig(
    core_name="plover",
    test_module="test_plover",
    here=HERE,
    root=ROOT,
    parameters={
        "VERSION_OVERRIDE":      VERSION_OVERRIDE,
        "VERSION_HASH_OVERRIDE": VERSION_HASH_OVERRIDE,
    },
    c_include_env="PLOVER_RDL_INCLUDE_DIRS",
    live_dir_map={"plover": PROJ_DIR},
)
TESTCASES = ["smoke", "firmware_smoke", "firmware_program_fir",
             "chain_impulse", "chain_tone"]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_plover(cocotb_testcase):
    run_testcase(CFG, cocotb_testcase)
