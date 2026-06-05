"""FuseSoC + pytest harness for channel_decimator."""
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


TESTCASES = ["impulse", "dc_step", "tone_passband", "tone_stopband"]

# (CIC_STAGES, CIC_DECIM, CIC_DELAY, SAMPLE_W, FIR_N_TAPS, FIR_COEF_W)
#
# Default: R=10, N=3 channel-rate decimation. Brings wide baseband
# (~2.4 MS/s) down to intermediate chain rate (~240 kS/s).
# Alt: R=8, N=2 — lighter CIC, different decimation factor.
PARAM_CONFIGS = [
    (3, 10, 1, 16, 32, 16),  # default channel-rate path
    (2,  8, 1, 16, 24, 16),  # alt: lighter CIC + fewer taps
]


def _cfg_for(params):
    (cic_stages, cic_decim, cic_delay,
     sample_w, fir_n_taps, fir_coef_w) = params
    fir_out_shift = fir_coef_w - 1
    os.environ["CHDEC_CIC_STAGES"]    = str(cic_stages)
    os.environ["CHDEC_CIC_DECIM"]     = str(cic_decim)
    os.environ["CHDEC_CIC_DELAY"]     = str(cic_delay)
    os.environ["CHDEC_SAMPLE_W"]      = str(sample_w)
    os.environ["CHDEC_FIR_N_TAPS"]    = str(fir_n_taps)
    os.environ["CHDEC_FIR_COEF_W"]    = str(fir_coef_w)
    os.environ["CHDEC_FIR_OUT_SHIFT"] = str(fir_out_shift)

    return HarnessConfig(
        core_name="channel_decimator",
        test_module="test_channel_decimator",
        here=HERE,
        root=ROOT,
        parameters={
            "CIC_STAGES":      cic_stages,
            "CIC_DECIM":       cic_decim,
            "CIC_DELAY":       cic_delay,
            "SAMPLE_W":        sample_w,
            "FIR_N_TAPS":      fir_n_taps,
            "FIR_COEF_W":      fir_coef_w,
            "FIR_OUT_SHIFT":   fir_out_shift,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize(
    "params", PARAM_CONFIGS,
    ids=lambda p: f"R{p[1]}_N{p[0]}_T{p[4]}")
def test_channel_decimator(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
