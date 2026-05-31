"""FuseSoC + pytest harness for cic_decimator.

Tiny shim over ``tools/dv_harness.py``. Sweeps a few parameter
combinations to confirm the model and RTL match across configurations,
not just the default.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]
sys.path.insert(0, str(ROOT))

from tools.dv_harness import HarnessConfig, run_testcase  # noqa: E402


TESTCASES = ["impulse", "step", "random_pattern", "backpressure"]

# Parameter sweep. Default config first; a couple of variants to confirm
# the Python model and RTL track each other across configurations.
# Format: (STAGES, DECIM, DELAY, IN_W, OUT_W).
PARAM_CONFIGS = [
    (3, 4, 1, 16, 16),  # default-ish: 3-stage decim-by-4
    (4, 8, 1, 16, 16),  # 4-stage, decim-by-8
    (3, 4, 2, 16, 16),  # M=2 (differential delay variant)
]


def _cfg_for(params):
    """Build a HarnessConfig that passes the params to Verilator AND
    exposes them to the cocotb test via env vars (so the Python model
    can match)."""
    stages, decim, delay, in_w, out_w = params

    import os
    # The shared harness uses extra_env at runtime; we set the env vars in
    # the parent process so the test_module picks them up. Pytest spawns
    # cocotb in the same process so this works directly.
    os.environ["CIC_STAGES"] = str(stages)
    os.environ["CIC_DECIM"]  = str(decim)
    os.environ["CIC_DELAY"]  = str(delay)
    os.environ["CIC_IN_W"]   = str(in_w)
    os.environ["CIC_OUT_W"]  = str(out_w)

    return HarnessConfig(
        core_name="cic_decimator",
        test_module="test_cic_decimator",
        here=HERE,
        root=ROOT,
        parameters={
            "STAGES": stages,
            "DECIM":  decim,
            "DELAY":  delay,
            "IN_W":   in_w,
            "OUT_W":  out_w,
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize("params", PARAM_CONFIGS,
                         ids=lambda p: f"N{p[0]}_R{p[1]}_M{p[2]}_W{p[3]}-{p[4]}")
def test_cic_decimator(cocotb_testcase, params):
    cfg = _cfg_for(params)
    run_testcase(cfg, cocotb_testcase)
