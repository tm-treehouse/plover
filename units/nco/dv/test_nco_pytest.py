"""FuseSoC + pytest harness for nco."""
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


TESTCASES = ["startup", "tone_slow", "tone_fast", "tone_arbitrary", "freq_update"]

# (SAMPLE_W, PHASE_W, LUT_N)
# Default plus a wider-LUT variant. The wider LUT exercises the
# table-build path and confirms bit-exact agreement against the Python
# model at higher LUT resolution (lower spur level — ~6*LUT_N dBc).
# PHASE_W stays at 32 across configs; the LUT index is just the top
# LUT_N bits of phase regardless of total width.
PARAM_CONFIGS = [
    (16, 32, 10),  # default — 1024-entry LUT (~60 dBc spurs)
    (16, 32, 12),  # 4096-entry LUT (~72 dBc spurs)
]


def _cfg_for(params):
    sample_w, phase_w, lut_n = params
    os.environ["NCO_SAMPLE_W"] = str(sample_w)
    os.environ["NCO_PHASE_W"]  = str(phase_w)
    os.environ["NCO_LUT_N"]    = str(lut_n)

    return HarnessConfig(
        core_name="nco",
        test_module="test_nco",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W": sample_w,
            "PHASE_W":  phase_w,
            "LUT_N":    lut_n,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}_P{p[1]}_L{p[2]}")
def test_nco(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
