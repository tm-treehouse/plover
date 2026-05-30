"""FuseSoC + pytest harness for axil_xbar.

Tiny shim over the shared harness in ``tools/dv_harness.py``. The xbar
needs three knobs the simpler units don't:

* hdl_toplevel="axil_xbar_dv_top" — DV wraps the xbar with two RAM stubs.
* extra_sources — the wrapper + the RAM stub aren't in the synthesizable
  RTL fileset, so the harness pulls them in directly.
* per-testcase parameters — sweeps (INPUT_REG_STAGES, OUTPUT_REG_STAGES)
  combinations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]
sys.path.insert(0, str(ROOT))

from tools.dv_harness import HarnessConfig, run_testcase  # noqa: E402


CFG = HarnessConfig(
    core_name="axil_xbar",
    test_module="test_axil_xbar",
    here=HERE,
    root=ROOT,
    hdl_toplevel="axil_xbar_dv_top",
    extra_sources=[
        UNIT_DIR / "dv" / "axil_ram_stub.sv",
        UNIT_DIR / "dv" / "axil_xbar_dv_top.sv",
    ],
)

TESTCASES = ["smoke", "decerr", "concurrent"]
# (INPUT_REG_STAGES, OUTPUT_REG_STAGES) configs to sweep. Three is enough
# to cover bypass, input-stage, and output-stage cases without exploding
# the run time. DEPTH=2 is just chained DEPTH=1 with no new logic paths.
STAGE_CONFIGS = [(0, 0), (1, 0), (0, 1)]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize("stages", STAGE_CONFIGS,
                         ids=lambda s: f"in{s[0]}_out{s[1]}")
def test_axil_xbar(cocotb_testcase, stages):
    run_testcase(CFG, cocotb_testcase, parameters={
        "INPUT_REG_STAGES":  stages[0],
        "OUTPUT_REG_STAGES": stages[1],
    })
