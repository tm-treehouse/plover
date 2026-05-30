"""FuseSoC + pytest harness for the counter sub-unit.

Tiny shim over the shared harness in ``tools/dv_harness.py``.
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

CFG = HarnessConfig(
    core_name="counter",
    test_module="test_counter",
    here=HERE,
    root=ROOT,
)
TESTCASES = ["smoke", "clear"]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_counter(cocotb_testcase):
    run_testcase(CFG, cocotb_testcase)
