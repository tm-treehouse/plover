"""FuseSoC + pytest harness for deemphasis."""
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


TESTCASES = ["dc_settle", "impulse", "tone_in_band", "tone_above"]

# (SAMPLE_W, COEF_W, COEF_INT_W, COEF_FRAC_W)
# Default Q1.15 plus a wider-coef config (Q2.16) for finer pole placement.
PARAM_CONFIGS = [
    (16, 16, 1, 15),
    (16, 18, 2, 16),
]


def _cfg_for(params):
    sample_w, coef_w, coef_int_w, coef_frac_w = params
    os.environ["DEMPH_SAMPLE_W"]    = str(sample_w)
    os.environ["DEMPH_COEF_W"]      = str(coef_w)
    os.environ["DEMPH_COEF_INT_W"]  = str(coef_int_w)
    os.environ["DEMPH_COEF_FRAC_W"] = str(coef_frac_w)

    return HarnessConfig(
        core_name="deemphasis",
        test_module="test_deemphasis",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W":    sample_w,
            "COEF_W":      coef_w,
            "COEF_INT_W":  coef_int_w,
            "COEF_FRAC_W": coef_frac_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"W{p[0]}_C{p[1]}_Q{p[2]}.{p[3]}")
def test_deemphasis(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
