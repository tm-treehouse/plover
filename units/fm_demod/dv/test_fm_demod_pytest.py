"""FuseSoC + pytest harness for fm_demod (smoke only this commit)."""
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


TESTCASES = ["smoke"]


def _cfg():
    sample_w = 16
    os.environ["FMD_SAMPLE_W"] = str(sample_w)
    return HarnessConfig(
        core_name="fm_demod",
        test_module="test_fm_demod",
        here=HERE,
        root=ROOT,
        parameters={
            "SAMPLE_W":           sample_w,
            "PHASE_W":            16,
            "CORDIC_ITER":        16,
            "AUDIO_CIC_STAGES":   3,
            "AUDIO_CIC_DECIM":    5,
            "AUDIO_FIR_N_TAPS":   16,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_fm_demod(cocotb_testcase):
    run_testcase(_cfg(), cocotb_testcase)
