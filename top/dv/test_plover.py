"""
cocotb entry point for the plover project-top integration testbench.

Scope is integration-level, not re-verification of the sub-units. The smoke
test exercises three things, in order:

1. **Two AXI paths.** Both AXI4-Lite slave ports are alive: read the
   axil_shell ID register (0x0C = 0xC0C07B01) via s_axil_*, and read the
   syscon VERSION register via s_syscon_* (parameter override sets it to
   a known value).

2. **The counter is wired and clocked.** Sample ``dut.count`` across
   several cycles and confirm it advances by the cycle count.

3. **Soft-reset gates the counter.** Write 1 to syscon's SOFT_RST.CORE
   (offset 0x08) over the syscon slave; syscon pulses soft_rst_n low for
   8 cycles, which holds the counter in reset. After that window passes
   the counter resumes from 0 and we confirm it.

If you regress the soft_rst_n wiring (e.g. tie it high in plover.sv), the
third check fails: the counter keeps counting through the soft-reset
window instead of snapping to 0.
"""
from __future__ import annotations

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from cocotbext.axi import AxiLiteBus, AxiLiteMaster

from pyuvm import uvm_test

from firmware_bridge import run_hello_world


CLK_PERIOD_NS = 10

# axil_shell ID register
SHELL_ID_OFFSET   = 0x0C
SHELL_ID_EXPECTED = 0xC0C07B01

# syscon VERSION (0x00), SOFT_RST (0x08). Override values match the
# parameters: dict in test_plover_pytest.py.
SYSCON_VERSION_OFFSET    = 0x00
SYSCON_SOFT_RST_OFFSET   = 0x08
EXPECTED_SYSCON_VERSION  = 0xCAFE_F00D
SYSCON_SOFT_RST_CYCLES   = 8       # syscon default; matches u_syscon.SOFT_RST_CYCLES


async def _start_clock_and_reset(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    # Idle every master-driven AXI input on both slaves through reset.
    for prefix in ("s_axil", "s_syscon"):
        for sig in (f"{prefix}_awvalid", f"{prefix}_wvalid", f"{prefix}_bready",
                    f"{prefix}_arvalid", f"{prefix}_rready"):
            if hasattr(dut, sig):
                getattr(dut, sig).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


def _make_master(dut, prefix: str) -> AxiLiteMaster:
    return AxiLiteMaster(
        AxiLiteBus.from_prefix(dut, prefix),
        dut.clk,
        dut.rst_n,
        reset_active_level=False,
    )


@pyuvm.test()
class smoke(uvm_test):
    """Integration smoke: two AXI paths, counter wiring, soft-reset gating."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            await _start_clock_and_reset(dut)

            shell  = _make_master(dut, "s_axil")
            syscon = _make_master(dut, "s_syscon")

            # ---- 1) Both AXI paths reach their slaves --------------------
            resp = await shell.read(SHELL_ID_OFFSET, 4)
            got = int.from_bytes(resp.data, "little")
            assert got == SHELL_ID_EXPECTED, (
                f"axil_shell ID register: got 0x{got:08x}, "
                f"expected 0x{SHELL_ID_EXPECTED:08x}")
            self.logger.info(
                f"axil_shell path OK: ID=0x{got:08x} via s_axil_*")

            resp = await syscon.read(SYSCON_VERSION_OFFSET, 4)
            got = int.from_bytes(resp.data, "little")
            assert got == EXPECTED_SYSCON_VERSION, (
                f"syscon VERSION register: got 0x{got:08x}, "
                f"expected 0x{EXPECTED_SYSCON_VERSION:08x}")
            self.logger.info(
                f"syscon path OK: VERSION=0x{got:08x} via s_syscon_*")

            # ---- 2) Counter is wired in and advancing --------------------
            await RisingEdge(dut.clk)
            start = int(dut.count.value)
            await ClockCycles(dut.clk, 10)
            end = int(dut.count.value)
            mask = (1 << len(dut.count)) - 1
            advanced = (end - start) & mask
            assert advanced == 10, (
                f"counter advance: expected 10 cycles, got {advanced} "
                f"(start=0x{start:x} end=0x{end:x})")
            self.logger.info(f"counter free-running OK: advanced {advanced}")

            # ---- 3) Soft-reset via syscon gates the counter --------------
            # Write 1 to SOFT_RST.CORE. syscon pulses soft_rst_n low for
            # SOFT_RST_CYCLES cycles; that holds the counter in reset and
            # zeros its output. After the window, the counter resumes from 0.
            await syscon.write(
                SYSCON_SOFT_RST_OFFSET, (1).to_bytes(4, "little"))
            # The write completes, then the pulse takes effect one cycle
            # later, then the counter is held for SOFT_RST_CYCLES. Sample
            # mid-window to confirm the counter is being held at 0.
            await ClockCycles(dut.clk, 3)
            mid = int(dut.count.value)
            assert mid == 0, (
                f"soft-reset gating: expected counter held at 0 mid-window, "
                f"got 0x{mid:x}")
            # Wait past the soft-reset window plus a couple of cycles, then
            # confirm the counter has started counting again from 0.
            await ClockCycles(dut.clk, SYSCON_SOFT_RST_CYCLES + 2)
            after = int(dut.count.value)
            assert 1 <= after <= SYSCON_SOFT_RST_CYCLES + 4, (
                f"soft-reset release: expected counter to be small and "
                f"counting again after the reset window, got 0x{after:x}")
            self.logger.info(
                f"soft-reset gating OK: counter held at 0, resumed to "
                f"0x{after:x} after the window")
        finally:
            self.drop_objection()


@pyuvm.test()
class firmware_smoke(uvm_test):
    """Drive the chip from the C++ host-side firmware (top/host/).

    Same wiring as `smoke` (clock, reset, two AXI-Lite masters), but the
    actual test logic lives in C++ and is called via ctypes. The C++ talks
    to the chip through host_ops callbacks that route through cocotb's
    bridge mechanism; from the C++'s perspective it's just calling
    ``shell_read(addr)`` and ``syscon_read(addr)``.

    This proves the C++ -> Python -> cocotb -> Verilator -> RTL chain
    works end-to-end. The actual checks are minimal (read shell ID and
    syscon VERSION) — the point of this test is the plumbing, not the
    coverage. Richer firmware-style tests can grow on top of the same
    bridge once the pattern's in place.
    """

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            await _start_clock_and_reset(dut)

            shell  = _make_master(dut, "s_axil")
            syscon = _make_master(dut, "s_syscon")

            rc = await run_hello_world(
                shell, syscon,
                expected_syscon_version=EXPECTED_SYSCON_VERSION,
            )
            assert rc == 0, f"plover_hello_world returned {rc} (non-zero = check failed)"
            self.logger.info("C++ firmware hello-world completed successfully")
        finally:
            self.drop_objection()
