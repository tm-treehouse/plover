"""FuseSoC + pytest harness for fir_filter."""
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


TESTCASES = ["impulse", "averaging", "arbitrary", "hot_update"]

# (N_TAPS, IN_W, COEF_W, OUT_W, OUT_SHIFT, COEF_INT_W, COEF_FRAC_W)
# COEF_INT_W + COEF_FRAC_W must equal COEF_W. With Q1.15 coefficients
# (the historical default), OUT_SHIFT = 15 = COEF_FRAC_W aligns the
# product back to the input's Q-position. The Q3.13 config below
# exercises the new Q-format parameterisation end-to-end: it asks the
# RTL to treat coefs as having 3 integer bits + 13 fractional bits,
# and shifts by 13 so the chain preserves Q1.15 samples. The test
# coefficients in plover_test.py / test_fir_filter.py are constructed
# from coef_w-1 (the largest positive representable value), so they
# naturally scale to whatever COEF_W is — the Q-format change is
# legible without rewriting the test bodies.
PARAM_CONFIGS = [
    (8,  16, 16, 16, 15, 1, 15),  # default — Q1.15 coefs
    (16, 16, 16, 16, 15, 1, 15),  # double the taps
    (4,  16, 16, 16, 15, 1, 15),  # short filter, exercises sum tree
    (8,  16, 16, 16, 13, 3, 13),  # Q3.13 coefs — exercises the Q params
]


def _cfg_for(params):
    n_taps, in_w, coef_w, out_w, out_shift, coef_int_w, coef_frac_w = params
    os.environ["FIR_N_TAPS"]      = str(n_taps)
    os.environ["FIR_IN_W"]        = str(in_w)
    os.environ["FIR_COEF_W"]      = str(coef_w)
    os.environ["FIR_OUT_W"]       = str(out_w)
    os.environ["FIR_OUT_SHIFT"]   = str(out_shift)
    os.environ["FIR_COEF_INT_W"]  = str(coef_int_w)
    os.environ["FIR_COEF_FRAC_W"] = str(coef_frac_w)

    return HarnessConfig(
        core_name="fir_filter",
        test_module="test_fir_filter",
        here=HERE,
        root=ROOT,
        parameters={
            "N_TAPS":      n_taps,
            "IN_W":        in_w,
            "COEF_W":      coef_w,
            "OUT_W":       out_w,
            "OUT_SHIFT":   out_shift,
            "COEF_INT_W":  coef_int_w,
            "COEF_FRAC_W": coef_frac_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"T{p[0]}_W{p[1]}-{p[2]}-{p[3]}_S{p[4]}_Q{p[5]}.{p[6]}")
def test_fir_filter(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
