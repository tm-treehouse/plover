"""FuseSoC + pytest harness for syscon.

Tiny shim over the shared harness in ``tools/dv_harness.py``. Passes
VERSION overrides at build time so VERSION/VERSION_HASH have
deterministic values for the scoreboard's reference model.
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


# Must match EXPECTED_VERSION / EXPECTED_VERSION_HASH in test_syscon.py.
VERSION_OVERRIDE      = 0xCAFE_F00D
VERSION_HASH_OVERRIDE = 0x1234_5678

CFG = HarnessConfig(
    core_name="syscon",
    test_module="test_syscon",
    here=HERE,
    root=ROOT,
    parameters={
        "VERSION_OVERRIDE":      VERSION_OVERRIDE,
        "VERSION_HASH_OVERRIDE": VERSION_HASH_OVERRIDE,
    },
)
TESTCASES = ["smoke", "reset_cause"]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_syscon(cocotb_testcase):
    run_testcase(CFG, cocotb_testcase)
