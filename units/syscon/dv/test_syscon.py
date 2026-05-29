"""cocotb entry point for the syscon unit testbench.

Same shape as units/axil_shell/dv/test_axil_shell.py: a small mixin wires
the live cocotb DUT handle and ClkRstIf into the env cfg, then defers to
the dv_lib base. Adds expected VERSION/VERSION_HASH values to the env cfg
so the scoreboard's reference model knows what to compare reads against —
these match the ``parameters`` passed to the Verilator build in the pytest
harness, so the test is deterministic regardless of git state.
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from syscon_env import SysconEnvCfg
from syscon_test import SysconBaseTest, SysconResetCauseTest
import syscon_test  # noqa: F401  -- registers vseqs by name

CLK_PERIOD_NS = 10
AXIL_PREFIX = "s_axil"

# These must match the parameters: passed to the runner in test_syscon_pytest.py.
EXPECTED_VERSION       = 0xCAFE_F00D
EXPECTED_VERSION_HASH  = 0x1234_5678


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = SysconEnvCfg("syscon_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        cfg.syscon_agent_cfg.vif = dut
        cfg.syscon_agent_cfg.prefix = AXIL_PREFIX
        cfg.version_value = EXPECTED_VERSION
        cfg.version_hash = EXPECTED_VERSION_HASH

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        ConfigDB().set(self, "env", "cfg", cfg)
        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, SysconBaseTest):
    pass


@pyuvm.test()
class reset_cause(_CfgWiringMixin, SysconResetCauseTest):
    pass
