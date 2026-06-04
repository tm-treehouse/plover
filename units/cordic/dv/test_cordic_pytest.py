"""FuseSoC + pytest harness for cordic."""
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


TESTCASES = ["cardinal_points", "slow_rotation", "random_samples", "unity_circle"]

# (SAMPLE_W, PHASE_W)
# Default plus a wider-phase variant exercising the Q-format
# parameterisation of the phase output.
PARAM_CONFIGS = [
    (16, 16),   # default — 16-bit samples, 16-bit phase
    (16, 20),   # wider phase output (finer phase resolution)
]


def _cfg_for(params):
    sample_w, phase_w = params
    os.environ["CORDIC_SAMPLE_W"] = str(sample_w)
    os.environ["CORDIC_PHASE_W"]  = str(phase_w)
    os.environ["CORDIC_ITERATIONS"] = "16"

    return HarnessConfig(
        core_name="cordic",
        test_module="test_cordic",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W":   sample_w,
            "PHASE_W":    phase_w,
            "ITERATIONS": 16,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}_P{p[1]}")
def test_cordic(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
