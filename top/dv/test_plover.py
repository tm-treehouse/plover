"""cocotb entry point for the plover project-top integration testbench.

After the xbar refactor the top exposes a single AXI4-Lite slave port
(s_axil_*) and one AXI4-Stream slave (s_axis_*). The xbar inside plover
routes AXI-Lite transactions by address:
    0x0000_0000 .. 0x0000_0FFF  ->  axil_shell  (4 KB page)
    0x0000_1000 .. 0x0000_1FFF  ->  syscon      (4 KB page)
Addresses outside those return AXI DECERR (resp = 2'b11).

Scope is integration-level, not re-verification of the sub-units. Four
@pyuvm.test() classes live here:

  smoke               — both pages reachable, counter wired, soft-reset
                        gates the counter, unmapped addresses return DECERR.
  firmware_smoke      — same wiring but the test logic runs in C from
                        top/host/ via the ctypes bridge.
  firmware_concurrent — cocotb AXIS stimulus runs in parallel with the
                        C firmware, both on independent buses.
"""
from __future__ import annotations

import os
from pathlib import Path

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from cocotbext.axi import AxiLiteBus, AxiLiteMaster
from cocotbext.axi import AxiStreamBus, AxiStreamSource

from pyuvm import uvm_test

from firmware_bridge import run_hello_world


CLK_PERIOD_NS = 10

# Page bases at the top boundary (xbar address map).
SHELL_BASE  = 0x0000_0000
SYSCON_BASE = 0x0000_1000
UNMAPPED    = 0x0000_2000   # neither page covers this; xbar returns DECERR

# axil_shell ID register, page-relative offset.
SHELL_ID_OFFSET   = 0x0C
SHELL_ID_EXPECTED = 0xC0C07B01

# syscon VERSION (0x00), SOFT_RST (0x08), page-relative offsets.
SYSCON_VERSION_OFFSET    = 0x00
SYSCON_SOFT_RST_OFFSET   = 0x08
EXPECTED_SYSCON_VERSION  = 0xCAFE_F00D
SYSCON_SOFT_RST_CYCLES   = 8       # syscon default; matches u_syscon.SOFT_RST_CYCLES

RESP_OKAY   = 0
RESP_DECERR = 3


async def _start_clock_and_reset(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    # Idle the single host-side AXI-Lite slave port through reset.
    for sig in ("s_axil_awvalid", "s_axil_wvalid", "s_axil_bready",
                "s_axil_arvalid", "s_axil_rready"):
        if hasattr(dut, sig):
            getattr(dut, sig).value = 0
    # Idle AXI-Stream input.
    for sig in ("s_axis_tvalid", "s_axis_tlast", "s_axis_tdata"):
        if hasattr(dut, sig):
            getattr(dut, sig).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


def _make_master(dut, prefix: str = "s_axil") -> AxiLiteMaster:
    return AxiLiteMaster(
        AxiLiteBus.from_prefix(dut, prefix),
        dut.clk,
        dut.rst_n,
        reset_active_level=False,
    )


def _include_dirs_from_env() -> list[Path]:
    raw = os.environ.get("PLOVER_RDL_INCLUDE_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


@pyuvm.test()
class smoke(uvm_test):
    """Integration smoke: both pages reachable, counter wired, soft-reset
    gates the counter, unmapped addresses return DECERR."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            await _start_clock_and_reset(dut)

            host = _make_master(dut)
            byte_lanes = host.write_if.byte_lanes

            # ---- 1) Both pages reachable through the xbar -------------
            resp = await host.read(SHELL_BASE + SHELL_ID_OFFSET, byte_lanes)
            got = int.from_bytes(resp.data, "little")
            assert resp.resp == RESP_OKAY, f"shell read resp {resp.resp}"
            assert got == SHELL_ID_EXPECTED, (
                f"axil_shell.ID via xbar: got 0x{got:08x}, "
                f"expected 0x{SHELL_ID_EXPECTED:08x}")
            self.logger.info(f"shell page OK: ID=0x{got:08x}")

            resp = await host.read(SYSCON_BASE + SYSCON_VERSION_OFFSET, byte_lanes)
            got = int.from_bytes(resp.data, "little")
            assert resp.resp == RESP_OKAY, f"syscon read resp {resp.resp}"
            assert got == EXPECTED_SYSCON_VERSION, (
                f"syscon.VERSION via xbar: got 0x{got:08x}, "
                f"expected 0x{EXPECTED_SYSCON_VERSION:08x}")
            self.logger.info(f"syscon page OK: VERSION=0x{got:08x}")

            # ---- 2) Unmapped addresses return DECERR -----------------
            wresp = await host.write(UNMAPPED, (0xDEADBEEF).to_bytes(byte_lanes, "little"))
            assert wresp.resp == RESP_DECERR, (
                f"write to unmapped 0x{UNMAPPED:08x}: got resp {wresp.resp}, "
                f"expected DECERR (3)")
            rresp = await host.read(UNMAPPED, byte_lanes)
            assert rresp.resp == RESP_DECERR, (
                f"read from unmapped 0x{UNMAPPED:08x}: got resp {rresp.resp}, "
                f"expected DECERR (3)")
            self.logger.info("DECERR OK: unmapped 0x%08x rejected" % UNMAPPED)

            # And the mapped pages still work after the DECERR (xbar
            # state machines aren't stuck on the rejected access).
            resp = await host.read(SHELL_BASE + SHELL_ID_OFFSET, byte_lanes)
            assert resp.resp == RESP_OKAY, "shell broken after DECERR"

            # ---- 3) Counter is wired in and advancing ----------------
            await RisingEdge(dut.clk)
            start = int(dut.count.value)
            await ClockCycles(dut.clk, 10)
            end = int(dut.count.value)
            mask = (1 << len(dut.count)) - 1
            advanced = (end - start) & mask
            assert advanced == 10, (
                f"counter advance: expected 10, got {advanced} "
                f"(start=0x{start:x} end=0x{end:x})")
            self.logger.info(f"counter free-running OK: advanced {advanced}")

            # ---- 4) Soft-reset via syscon gates the counter ----------
            await host.write(
                SYSCON_BASE + SYSCON_SOFT_RST_OFFSET,
                (1).to_bytes(byte_lanes, "little"))
            await ClockCycles(dut.clk, 3)
            mid = int(dut.count.value)
            assert mid == 0, (
                f"soft-reset gating: expected counter held at 0 mid-window, "
                f"got 0x{mid:x}")
            await ClockCycles(dut.clk, SYSCON_SOFT_RST_CYCLES + 2)
            after = int(dut.count.value)
            assert 1 <= after <= SYSCON_SOFT_RST_CYCLES + 4, (
                f"soft-reset release: expected counter small after window, "
                f"got 0x{after:x}")
            self.logger.info(
                f"soft-reset gating OK: held at 0 mid-pulse, "
                f"resumed to 0x{after:x} after")
        finally:
            self.drop_objection()


@pyuvm.test()
class firmware_smoke(uvm_test):
    """Drive the chip from the C host-side firmware (top/host/).

    Same wiring as `smoke` but the actual test logic lives in C and is
    called via ctypes through one AXI-Lite master. The C calls into
    host_ops callbacks that route through cocotb's bridge mechanism.
    Proves C -> Python -> cocotb -> Verilator -> RTL works end-to-end.
    """

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            await _start_clock_and_reset(dut)

            host = _make_master(dut)
            include_dirs = _include_dirs_from_env()

            rc = await run_hello_world(
                host,
                shell_base=SHELL_BASE,
                syscon_base=SYSCON_BASE,
                expected_syscon_version=EXPECTED_SYSCON_VERSION,
                include_dirs=include_dirs,
            )
            assert rc == 0, f"plover_hello_world returned {rc} (non-zero = check failed)"
            self.logger.info("C firmware hello-world completed successfully")
        finally:
            self.drop_objection()


@pyuvm.test()
class firmware_concurrent(uvm_test):
    """Run cocotb AXI-Stream stimulus AND the C firmware at the same time.

    Both stimuli sources run live in the same simulator run on independent
    buses:
      * cocotb's AxiStreamSource pushes N beats into stream_sink via
        s_axis_*, started with cocotb.start_soon so it runs concurrently.
      * The C firmware does its register-access work on the single
        s_axil_* port via the bridge.

    Concurrency check at the end: C returned 0, sink received expected
    beats and XOR, AND at least one beat landed during the firmware run.
    """

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            await _start_clock_and_reset(dut)

            host = _make_master(dut)
            axis_src = AxiStreamSource(
                AxiStreamBus.from_prefix(dut, "s_axis"),
                dut.clk, dut.rst_n, reset_active_level=False,
            )

            stream_pattern = [
                0x00000001, 0x00000002, 0x00000004, 0x00000008,
                0xDEADBEEF, 0x12345678, 0xA5A5A5A5, 0x5A5A5A5A,
                0xC0FFEE00, 0xBADC0DE1, 0xFEEDFACE, 0xDEADC0DE,
                0x11111111, 0x22222222, 0x33333333, 0x44444444,
            ]
            expected_beats = len(stream_pattern)
            expected_xor = 0
            for w in stream_pattern:
                expected_xor ^= w

            byte_lanes = len(dut.s_axis_tdata) // 8

            async def push_stream() -> None:
                for w in stream_pattern:
                    await axis_src.send(w.to_bytes(byte_lanes, "little"))
                await axis_src.wait()

            stream_task = cocotb.start_soon(push_stream())
            include_dirs = _include_dirs_from_env()

            rc = await run_hello_world(
                host,
                shell_base=SHELL_BASE,
                syscon_base=SYSCON_BASE,
                expected_syscon_version=EXPECTED_SYSCON_VERSION,
                include_dirs=include_dirs,
            )
            assert rc == 0, f"plover_hello_world returned {rc} (non-zero = check failed)"

            mid_run_beats = int(dut.sink_beat_count.value)
            assert mid_run_beats > 0, (
                f"expected AXIS to have pushed beats during the C "
                f"firmware execution, but sink_beat_count = {mid_run_beats}")
            self.logger.info(
                f"during-firmware probe: sink_beat_count = {mid_run_beats} "
                f"(confirms AXIS ran concurrently with C)")

            await stream_task
            await ClockCycles(dut.clk, 3)

            beat_count = int(dut.sink_beat_count.value)
            data_xor   = int(dut.sink_data_xor.value)
            self.logger.info(
                f"sink final state: beat_count={beat_count} "
                f"data_xor=0x{data_xor:08x}")
            assert beat_count == expected_beats, (
                f"AXIS beat_count: got {beat_count}, expected {expected_beats}")
            assert data_xor == expected_xor, (
                f"AXIS data_xor: got 0x{data_xor:08x}, "
                f"expected 0x{expected_xor:08x}")
            self.logger.info(
                "concurrent test OK: C firmware completed AND AXIS sink "
                "received the expected stimulus")
        finally:
            self.drop_objection()
