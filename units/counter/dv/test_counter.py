"""
cocotb entry point for the counter unit testbench.

Mirrors ``dv/axil_shell/test_axil_shell.py``: a small mixin wires the live
DUT handle + ClkRstIf into the env cfg in ``build_phase``, then defers to
the dv_lib base. The run-phase objection lives in :class:`CounterBaseTest`,
not here, so the entry has no run_phase logic.

Tests exposed to cocotb (selectable with pytest's ``-k``):
* ``smoke`` -> CounterSmokeVSeq
* ``clear`` -> CounterClearVSeq
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from counter_env import CounterEnvCfg
from counter_test import CounterBaseTest, CounterClearTest
import counter_test  # noqa: F401  -- registers vseqs by name

CLK_PERIOD_NS = 10
WIDTH = 8


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = CounterEnvCfg("counter_env_cfg")
        cfg.width = WIDTH
        cfg.initialize()
        cfg.vif = dut

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        ConfigDB().set(self, "env", "cfg", cfg)
        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, CounterBaseTest):
    pass


@pyuvm.test()
class clear(_CfgWiringMixin, CounterClearTest):
    pass
