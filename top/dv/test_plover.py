"""cocotb entry point for the plover top integration testbench.

After the DSP chain integration, the top exposes:
  * an AXI4-Lite host port (xbar fans to axil_shell / syscon / fir_filter)
  * an AXIS-in port carrying samples into the CIC-FIR chain
  * an AXIS-out port carrying the filtered chain output
  * a counter debug output

The mixin wires the live DUT handle + ClkRstIf into the env cfg in
``build_phase``, then defers to the dv_lib base. Five pyuvm tests:

* ``smoke``                — control-plane: register reads, DECERR,
                              CONTROL.ENABLE gating, soft-reset
* ``firmware_smoke``       — C plover_hello_world via the firmware bridge
* ``firmware_program_fir`` — C programs FIR coefs, then samples flow
                              through the chain; scoreboard verifies
* ``chain_impulse``        — DSP-aware: delta filter + impulse stream
* ``chain_tone``           — DSP-aware: averager + sinusoidal stream

Per-test settable bits (``test_seq_s``, ``enable_chain_check``,
``drain_cycles``) live on PloverBaseTest and its subclasses in
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
    PloverBaseTest,
    PloverFirmwareSmokeTest,
    PloverFirmwareProgramFirTest,
    PloverChainImpulseTest,
    PloverChainToneTest,
)
import plover_test  # noqa: F401  -- registers vseqs by name

CLK_PERIOD_NS = 10


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = PloverEnvCfg("plover_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        # The env's build_phase propagates vif and prefixes to each
        # agent cfg; nothing else to wire here.
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
class firmware_program_fir(_CfgWiringMixin, PloverFirmwareProgramFirTest):
    pass


@pyuvm.test()
class chain_impulse(_CfgWiringMixin, PloverChainImpulseTest):
    pass


@pyuvm.test()
class chain_tone(_CfgWiringMixin, PloverChainToneTest):
    pass
