"""FuseSoC + pytest harness for complex_mixer."""
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


TESTCASES = ["unity_passthrough", "j_rotation", "random_streams", "dc_x_tone"]

# (SAMPLE_W, SAMPLE_INT_W, SAMPLE_FRAC_W, OUT_SHIFT)
# Default Q1.15 plus Q3.13 variant exercising the Q-format params.
PARAM_CONFIGS = [
    (16, 1, 15, 15),  # default — Q1.15 samples, shift by frac
    (16, 3, 13, 13),  # Q3.13 samples (headroom), shift by frac
]


def _cfg_for(params):
    sample_w, int_w, frac_w, out_shift = params
    os.environ["MIX_SAMPLE_W"]      = str(sample_w)
    os.environ["MIX_SAMPLE_INT_W"]  = str(int_w)
    os.environ["MIX_SAMPLE_FRAC_W"] = str(frac_w)
    os.environ["MIX_OUT_SHIFT"]     = str(out_shift)

    return HarnessConfig(
        core_name="complex_mixer",
        test_module="test_complex_mixer",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W":      sample_w,
            "SAMPLE_INT_W":  int_w,
            "SAMPLE_FRAC_W": frac_w,
            "OUT_SHIFT":     out_shift,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}_Q{p[1]}.{p[2]}_S{p[3]}")
def test_complex_mixer(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
