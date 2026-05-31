"""FuseSoC + pytest harness for axil_shell.

Tiny shim over the shared harness in ``tools/dv_harness.py``. Adding a
new unit is the same shape: change the core_name, test_module, test list.
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
    core_name="axil_shell",
    test_module="test_axil_shell",
    here=HERE,
    root=ROOT,
)
TESTCASES = ["smoke", "sweep", "control_ports"]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_axil_shell(cocotb_testcase):
    run_testcase(CFG, cocotb_testcase)
