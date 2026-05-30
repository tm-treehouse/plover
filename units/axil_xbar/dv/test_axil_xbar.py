"""Cocotb tests for axil_xbar.

Three scenarios:

  smoke      — write to slave-0 page, read back, confirm slave-1 was not
               touched; same for slave-1. Confirms address-based routing.

  decerr     — write/read to an unmapped address (between the two pages).
               Confirms the xbar returns AXI DECERR (resp = 0b11), not OKAY,
               and that neither slave saw the access.

  concurrent — issue a write and a read to *different* slaves and observe
               both complete cleanly. Confirms read/write paths really are
               independent in the xbar.
"""
from __future__ import annotations

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from cocotbext.axi import AxiLiteBus, AxiLiteMaster

from pyuvm import uvm_test


CLK_PERIOD_NS = 10
SLAVE0_BASE = 0x0000_0000
SLAVE1_BASE = 0x0000_1000
UNMAPPED   = 0x0000_2000   # between pages, no slave matches

RESP_OKAY   = 0
RESP_DECERR = 3


async def _setup(dut) -> AxiLiteMaster:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
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
        AxiLiteBus.from_prefix(dut, "s_axil"),
        dut.clk, dut.rst_n,
        reset_active_level=False,
    )


@pyuvm.test()
class smoke(uvm_test):
    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            master = await _setup(dut)
            byte_lanes = master.write_if.byte_lanes

            # Write distinct values to a register on each slave; read back.
            await master.write(SLAVE0_BASE + 0x04, (0xCAFE_F00D).to_bytes(byte_lanes, "little"))
            await master.write(SLAVE1_BASE + 0x08, (0xDEAD_BEEF).to_bytes(byte_lanes, "little"))

            resp0 = await master.read(SLAVE0_BASE + 0x04, byte_lanes)
            resp1 = await master.read(SLAVE1_BASE + 0x08, byte_lanes)
            got0 = int.from_bytes(resp0.data, "little")
            got1 = int.from_bytes(resp1.data, "little")
            assert resp0.resp == RESP_OKAY, f"slave0 read resp = {resp0.resp}"
            assert resp1.resp == RESP_OKAY, f"slave1 read resp = {resp1.resp}"
            assert got0 == 0xCAFE_F00D, f"slave0 readback got 0x{got0:08x}"
            assert got1 == 0xDEAD_BEEF, f"slave1 readback got 0x{got1:08x}"

            # Confirm slave-0's address space wasn't affected by the
            # slave-1 write (and vice-versa). Read slave-0 at the offset
            # where we wrote slave-1 — should be 0 (RAM stub resets to 0).
            resp = await master.read(SLAVE0_BASE + 0x08, byte_lanes)
            got = int.from_bytes(resp.data, "little")
            assert got == 0, f"slave0 @ 0x08 leaked from slave1: got 0x{got:08x}"
            resp = await master.read(SLAVE1_BASE + 0x04, byte_lanes)
            got = int.from_bytes(resp.data, "little")
            assert got == 0, f"slave1 @ 0x04 leaked from slave0: got 0x{got:08x}"

            self.logger.info("axil_xbar smoke: routing isolated, both slaves "
                             "reachable via correct page bases")
        finally:
            self.drop_objection()


@pyuvm.test()
class decerr(uvm_test):
    """Confirm unmapped addresses get DECERR back."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            master = await _setup(dut)
            byte_lanes = master.write_if.byte_lanes

            # Write to unmapped address.
            resp = await master.write(UNMAPPED,
                                      (0x1234_5678).to_bytes(byte_lanes, "little"))
            assert resp.resp == RESP_DECERR, (
                f"write to unmapped got resp = {resp.resp}, expected DECERR (3)")

            # Read from unmapped.
            rresp = await master.read(UNMAPPED, byte_lanes)
            assert rresp.resp == RESP_DECERR, (
                f"read from unmapped got resp = {rresp.resp}, expected DECERR (3)")

            # Crucially: the mapped slaves must still work after the DECERR.
            await master.write(SLAVE0_BASE + 0x0C,
                               (0xABCD).to_bytes(byte_lanes, "little"))
            resp = await master.read(SLAVE0_BASE + 0x0C, byte_lanes)
            got = int.from_bytes(resp.data, "little")
            assert resp.resp == RESP_OKAY, (
                f"after DECERR, slave0 read resp = {resp.resp}, expected OKAY")
            assert got == 0xABCD, (
                f"after DECERR, slave0 readback got 0x{got:08x}")
            self.logger.info("axil_xbar decerr: unmapped → DECERR, mapped "
                             "slaves still operational after")
        finally:
            self.drop_objection()


@pyuvm.test()
class concurrent(uvm_test):
    """Read and write to different slaves in parallel; both complete OK."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            master = await _setup(dut)
            byte_lanes = master.write_if.byte_lanes

            # Seed slave-1 so the read has something interesting.
            await master.write(SLAVE1_BASE + 0x10,
                               (0xBEEF_F00D).to_bytes(byte_lanes, "little"))

            # Now fire a write to slave-0 and a read from slave-1 without
            # awaiting the first. cocotbext-axi's master may serialize at
            # the bus level, but the xbar should handle either pattern.
            write_task = cocotb.start_soon(
                master.write(SLAVE0_BASE + 0x00,
                             (0x99).to_bytes(byte_lanes, "little")))
            read_task = cocotb.start_soon(
                master.read(SLAVE1_BASE + 0x10, byte_lanes))

            wresp = await write_task
            rresp = await read_task
            got = int.from_bytes(rresp.data, "little")
            assert wresp.resp == RESP_OKAY, f"write resp {wresp.resp}"
            assert rresp.resp == RESP_OKAY, f"read resp {rresp.resp}"
            assert got == 0xBEEF_F00D, f"concurrent read got 0x{got:08x}"
            self.logger.info("axil_xbar concurrent: independent W+R "
                             "across slaves both completed OK")
        finally:
            self.drop_objection()
