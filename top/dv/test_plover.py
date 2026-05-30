"""cocotb entry point for the plover top integration testbench.

Same shape as the unit testbenches: a small mixin wires the live DUT
handle + ClkRstIf into the env cfg in ``build_phase``, then defers to
the dv_lib base. Three pyuvm tests:

* ``smoke``               -> PloverSmokeVSeq
* ``firmware_smoke``      -> PloverFirmwareSmokeVSeq
* ``firmware_concurrent`` -> PloverFirmwareConcurrentVSeq

Per-test settable bits (``test_seq_s``, ``expect_axis_check``,
``settle_cycles``) live on PloverBaseTest and its subclasses in
plover_test.py.

VERSION_OVERRIDE / VERSION_HASH_OVERRIDE come into the DUT via the
pytest harness (test_plover_pytest.py). EXPECTED_SYSCON_VERSION in
plover_test.py must match what the harness passes.
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from plover_env import PloverEnvCfg
from plover_test import (
    PloverBaseTest, PloverFirmwareSmokeTest, PloverFirmwareConcurrentTest,
)
import plover_test  # noqa: F401  -- registers vseqs by name

CLK_PERIOD_NS = 10
AXIL_PREFIX = "s_axil"
AXIS_PREFIX = "s_axis"


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = PloverEnvCfg("plover_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        assert cfg.axil_agent_cfg is not None
        assert cfg.axis_agent_cfg is not None
        cfg.axil_agent_cfg.vif = dut
        cfg.axil_agent_cfg.prefix = AXIL_PREFIX
        cfg.axis_agent_cfg.vif = dut
        cfg.axis_agent_cfg.prefix = AXIS_PREFIX

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        ConfigDB().set(self, "env", "cfg", cfg)
        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, PloverBaseTest):
    pass


@pyuvm.test()
class firmware_smoke(_CfgWiringMixin, PloverFirmwareSmokeTest):
    pass


@pyuvm.test()
class firmware_concurrent(_CfgWiringMixin, PloverFirmwareConcurrentTest):
    pass
