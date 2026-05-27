"""
cocotb entry point for the AXI-Lite shell testbench.

Uses the documented dv_lib entry pattern: decorate the project's test class
with ``@pyuvm.test()``. The only extra work the entry does is wire the live
cocotb DUT handle (``cocotb.top``) and a :class:`ClkRstIf` into the env cfg;
the run-phase objection is owned by ``AxilBaseTest.run_phase`` (the standard
pyuvm idiom), so there is no run_phase logic here.

Why wire the cfg in build_phase: ``@pyuvm.test()`` runs the test through
``uvm_root().run_test(cls)``, which clears singletons (the ConfigDB included)
as it starts — so the cfg must be installed from inside the test, once the
simulator (and therefore ``cocotb.top``) is live.

Select a vseq with ``+UVM_TEST_SEQ=...`` or run the matching test below:
* ``smoke`` -> AxilSmokeVSeq
* ``sweep`` -> AxilSweepVSeq
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from axil_shell.axil_env import AxilEnvCfg
from axil_shell.axil_test import AxilBaseTest, AxilSweepTest
# Importing the vseq module registers the vseqs as DVBaseSequence subclasses
# so DVBaseTest.create_seq_by_name() can resolve them by name.
from axil_shell import axil_test  # noqa: F401

CLK_PERIOD_NS = 10
AXIL_PREFIX = "s_axil"


class _CfgWiringMixin:
    """Builds + wires the env cfg from the live DUT, then defers to dv_lib.

    Mixed in ahead of the project test class so this build_phase runs first,
    installs the cfg into the ConfigDB at the ``env`` scope the base reads
    from, and starts the clock. No run_phase here — objections live in
    AxilBaseTest.run_phase.
    """

    def build_phase(self) -> None:
        dut = cocotb.top  # live DUT handle (sim is running by build_phase)

        cfg = AxilEnvCfg("axil_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        cfg.axil_agent_cfg.vif = dut
        cfg.axil_agent_cfg.prefix = AXIL_PREFIX

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        # DVBaseTest.build_phase reads cfg from ConfigDB().get(self, "env", "cfg").
        ConfigDB().set(self, "env", "cfg", cfg)

        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, AxilBaseTest):
    pass


@pyuvm.test()
class sweep(_CfgWiringMixin, AxilSweepTest):
    pass
