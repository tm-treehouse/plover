"""FuseSoC + pytest harness for phase_diff."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]
sys.path.insert(0, str(ROOT))

from tools.dv_harness import HarnessConfig, run_testcase  # noqa: E402


TESTCASES = ["linear_ramp", "phase_wrap", "random_phases", "constant_phase"]

# (PHASE_W,)
# Default 16-bit; the second config exercises a wider phase width to
# validate the Q-format parameterisation. PHASE_W must be a multiple
# of 8 because the AXIS BFM requires byte-aligned bus widths and
# this unit has no padding (single-component TDATA).
PARAM_CONFIGS = [
    (16,),
    (24,),
]


def _cfg_for(params):
    (phase_w,) = params
    os.environ["PD_PHASE_W"] = str(phase_w)

    return HarnessConfig(
        core_name="phase_diff",
        test_module="test_phase_diff",
        here=HERE,
        root=ROOT,
        parameters={"PHASE_W": phase_w},
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"P{p[0]}")
def test_phase_diff(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
