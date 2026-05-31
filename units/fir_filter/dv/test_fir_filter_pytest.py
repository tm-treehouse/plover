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

PARAM_CONFIGS = [
    # (N_TAPS, IN_W, COEF_W, OUT_W, OUT_SHIFT)
    (8,  16, 16, 16, 15),  # default
    (16, 16, 16, 16, 15),  # double the taps
    (4,  16, 16, 16, 15),  # short filter, exercises the sum tree size
]


def _cfg_for(params):
    n_taps, in_w, coef_w, out_w, out_shift = params
    os.environ["FIR_N_TAPS"]    = str(n_taps)
    os.environ["FIR_IN_W"]      = str(in_w)
    os.environ["FIR_COEF_W"]    = str(coef_w)
    os.environ["FIR_OUT_W"]     = str(out_w)
    os.environ["FIR_OUT_SHIFT"] = str(out_shift)

    return HarnessConfig(
        core_name="fir_filter",
        test_module="test_fir_filter",
        here=HERE,
        root=ROOT,
        parameters={
            "N_TAPS":    n_taps,
            "IN_W":      in_w,
            "COEF_W":    coef_w,
            "OUT_W":     out_w,
            "OUT_SHIFT": out_shift,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize("params", PARAM_CONFIGS,
                         ids=lambda p: f"T{p[0]}_W{p[1]}-{p[2]}-{p[3]}_S{p[4]}")
def test_fir_filter(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
