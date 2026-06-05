"""FuseSoC + pytest harness for audio_decimator."""
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
# Default: R=5, N=3 audio-rate decimation (~250 kS/s -> ~50 kS/s).
# Alt: R=4, N=2 — exercises a different decimation factor and stage count.
PARAM_CONFIGS = [
    (3, 5, 1, 16, 16, 16),   # default audio-rate path
    (2, 4, 1, 16, 12, 16),   # alt: lighter CIC, fewer FIR taps
]


def _cfg_for(params):
    (cic_stages, cic_decim, cic_delay,
     sample_w, fir_n_taps, fir_coef_w) = params
    fir_out_shift = fir_coef_w - 1   # default to COEF_FRAC_W
    os.environ["AUDEC_CIC_STAGES"]    = str(cic_stages)
    os.environ["AUDEC_CIC_DECIM"]     = str(cic_decim)
    os.environ["AUDEC_CIC_DELAY"]     = str(cic_delay)
    os.environ["AUDEC_SAMPLE_W"]      = str(sample_w)
    os.environ["AUDEC_FIR_N_TAPS"]    = str(fir_n_taps)
    os.environ["AUDEC_FIR_COEF_W"]    = str(fir_coef_w)
    os.environ["AUDEC_FIR_OUT_SHIFT"] = str(fir_out_shift)

    return HarnessConfig(
        core_name="audio_decimator",
        test_module="test_audio_decimator",
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
def test_audio_decimator(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
