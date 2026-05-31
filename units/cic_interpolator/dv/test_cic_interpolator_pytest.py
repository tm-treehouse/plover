"""FuseSoC + pytest harness for cic_interpolator."""
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


TESTCASES = ["impulse", "step", "random_pattern", "backpressure"]

PARAM_CONFIGS = [
    (3, 4, 1, 16, 16),  # default
    (4, 8, 1, 16, 16),  # deeper, higher interp
    (3, 4, 2, 16, 16),  # M=2
]


def _cfg_for(params):
    stages, interp, delay, in_w, out_w = params
    os.environ["CIC_STAGES"] = str(stages)
    os.environ["CIC_INTERP"] = str(interp)
    os.environ["CIC_DELAY"]  = str(delay)
    os.environ["CIC_IN_W"]   = str(in_w)
    os.environ["CIC_OUT_W"]  = str(out_w)

    return HarnessConfig(
        core_name="cic_interpolator",
        test_module="test_cic_interpolator",
        here=HERE,
        root=ROOT,
        parameters={
            "STAGES": stages,
            "INTERP": interp,
            "DELAY":  delay,
            "IN_W":   in_w,
            "OUT_W":  out_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize("params", PARAM_CONFIGS,
                         ids=lambda p: f"N{p[0]}_R{p[1]}_M{p[2]}_W{p[3]}-{p[4]}")
def test_cic_interpolator(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
