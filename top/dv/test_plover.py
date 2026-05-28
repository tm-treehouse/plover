"""
cocotb entry point for the plover project-top integration testbench.

Scope is deliberately narrow: this is not a re-verification of axil_shell or
counter (those have their own unit testbenches under ``units/``). It checks
that the integration is wired and alive:

* AXI4-Lite transactions issued at the top boundary reach the shell
  through ``plover.sv``'s hierarchy. We confirm by reading the shell's
  constant ID register (0x0C = 0xC0C07B01).
* The counter sub-unit, instantiated inside plover, is actually running.
  We confirm by sampling ``dut.count`` across a few cycles and watching it
  advance.

Follow-up (noted in plover.sv): once the shell exposes its CONTROL bits as
ports, the counter's ``enable``/``clear`` will be sourced from
``CONTROL.ENABLE`` / a CONTROL spare bit, and this testbench grows a check
of the form "write CONTROL.ENABLE=0, confirm count freezes."
"""
from __future__ import annotations

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from cocotbext.axi import AxiLiteBus, AxiLiteMaster

from pyuvm import uvm_test


CLK_PERIOD_NS = 10
AXIL_PREFIX = "s_axil"
ID_OFFSET = 0x0C
ID_EXPECTED = 0xC0C07B01


async def _setup(dut) -> AxiLiteMaster:
    """Start the clock, apply reset, return a configured AXI-Lite master."""
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())

    # Idle master-driven inputs through reset.
    for sig in ("s_axil_awvalid", "s_axil_wvalid", "s_axil_bready",
                "s_axil_arvalid", "s_axil_rready"):
        if hasattr(dut, sig):
            getattr(dut, sig).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    return AxiLiteMaster(
        AxiLiteBus.from_prefix(dut, AXIL_PREFIX),
        dut.clk,
        dut.rst_n,
        reset_active_level=False,
    )


@pyuvm.test()
class smoke(uvm_test):
    """Integration smoke: AXI path reaches the shell, and the counter runs."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            master = await _setup(dut)

            # ---- 1) AXI path through the hierarchy --------------------
            # Reading the shell's constant ID register exercises the entire
            # axil_shell -> plover boundary; if anything in the wiring is
            # wrong, this read will mismatch or hang.
            resp = await master.read(ID_OFFSET, 4)
            got = int.from_bytes(resp.data, "little")
            assert got == ID_EXPECTED, (
                f"ID register mismatch through plover top: "
                f"got 0x{got:08x}, expected 0x{ID_EXPECTED:08x}")
            self.logger.info(
                f"AXI integration check: read ID=0x{got:08x} via plover top — OK")

            # ---- 2) Counter is wired in and advancing -----------------
            # With the current placeholder wiring (counter_enable=1,
            # counter_clear=0 in plover.sv) the counter should be free-running.
            await RisingEdge(dut.clk)
            start = int(dut.count.value)
            await ClockCycles(dut.clk, 10)
            end = int(dut.count.value)
            advanced = (end - start) & ((1 << len(dut.count)) - 1)
            assert advanced == 10, (
                f"Counter wiring check: expected count to advance by 10 over "
                f"10 cycles, got {advanced} (start=0x{start:x} end=0x{end:x})")
            self.logger.info(
                f"Counter wiring check: advanced {advanced} over 10 cycles — OK")
        finally:
            self.drop_objection()
