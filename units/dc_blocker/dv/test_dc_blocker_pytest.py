"""FuseSoC + pytest harness for dc_blocker."""
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


TESTCASES = ["dc_step", "impulse", "sinusoid", "alpha_update"]

# (IN_W, COEF_W, OUT_W, COEF_INT_W, COEF_FRAC_W)
# Default Q1.15 plus one wider-coef config exercising the Q-format
# machinery — same pattern as the FIR's Q3.13 sweep, but here a Q2.16
# alpha (wider coef = finer pole positioning).
PARAM_CONFIGS = [
    (16, 16, 16, 1, 15),  # default — Q1.15 coefficient
    (16, 18, 16, 2, 16),  # Q2.16 coefficient (finer pole positioning)
]


def _cfg_for(params):
    in_w, coef_w, out_w, coef_int_w, coef_frac_w = params
    os.environ["DCB_IN_W"]        = str(in_w)
    os.environ["DCB_COEF_W"]      = str(coef_w)
    os.environ["DCB_OUT_W"]       = str(out_w)
    os.environ["DCB_COEF_INT_W"]  = str(coef_int_w)
    os.environ["DCB_COEF_FRAC_W"] = str(coef_frac_w)

    return HarnessConfig(
        core_name="dc_blocker",
        test_module="test_dc_blocker",
        here=HERE,
        root=ROOT,
        parameters={
            "IN_W":        in_w,
            "COEF_W":      coef_w,
            "OUT_W":       out_w,
            "COEF_INT_W":  coef_int_w,
            "COEF_FRAC_W": coef_frac_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}-{p[1]}-{p[2]}_Q{p[3]}.{p[4]}")
def test_dc_blocker(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
