"""cocotb entry point for the axil_xbar unit testbench.

Same shape as the other dv_lib-based unit testbenches: a small mixin
wires the live DUT handle + ClkRstIf into the env cfg in ``build_phase``,
then defers to the dv_lib base. Three pyuvm tests:

* ``smoke``      — write+read each page, cross-slave isolation probes.
* ``decerr``     — unmapped → DECERR, mapped recovery after.
* ``concurrent`` — back-to-back write-to-A + read-from-B (xbar's split
                   read/write FSMs let cocotbext-axi pipeline freely).

The DV harness instantiates this via the axil_xbar_dv_top wrapper, which
wires the xbar to two RAM stubs at 0x0000_0000 and 0x0000_1000.
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from axil_xbar_env import AxilXbarEnvCfg
from axil_xbar_test import (
    AxilXbarBaseTest, AxilXbarDecerrTest, AxilXbarConcurrentTest,
)
import axil_xbar_test  # noqa: F401  -- registers vseqs by name

CLK_PERIOD_NS = 10
AXIL_PREFIX = "s_axil"


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = AxilXbarEnvCfg("axil_xbar_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        assert cfg.axil_agent_cfg is not None
        cfg.axil_agent_cfg.vif = dut
        cfg.axil_agent_cfg.prefix = AXIL_PREFIX

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        ConfigDB().set(self, "env", "cfg", cfg)
        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, AxilXbarBaseTest):
    pass


@pyuvm.test()
class decerr(_CfgWiringMixin, AxilXbarDecerrTest):
    pass


@pyuvm.test()
class concurrent(_CfgWiringMixin, AxilXbarConcurrentTest):
    pass
