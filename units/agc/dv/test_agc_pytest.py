"""FuseSoC + pytest harness for agc."""
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


TESTCASES = ["small_signal", "large_signal", "random_streams", "gain_clamp"]

# (SAMPLE_W, GAIN_W, GAIN_INT_W, GAIN_FRAC_W)
# Default Q4.12 gain plus a wider-gain config exercising the Q params.
PARAM_CONFIGS = [
    (16, 16, 4, 12),   # default — Q4.12 gain
    (16, 20, 6, 14),   # wider gain — Q6.14 (range ~64, finer resolution)
]


def _cfg_for(params):
    sample_w, gain_w, gain_int_w, gain_frac_w = params
    os.environ["AGC_SAMPLE_W"]    = str(sample_w)
    os.environ["AGC_GAIN_W"]      = str(gain_w)
    os.environ["AGC_GAIN_INT_W"]  = str(gain_int_w)
    os.environ["AGC_GAIN_FRAC_W"] = str(gain_frac_w)

    return HarnessConfig(
        core_name="agc",
        test_module="test_agc",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W":    sample_w,
            "GAIN_W":      gain_w,
            "GAIN_INT_W":  gain_int_w,
            "GAIN_FRAC_W": gain_frac_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}_G{p[1]}_Q{p[2]}.{p[3]}")
def test_agc(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
