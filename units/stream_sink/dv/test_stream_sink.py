"""Cocotb test for stream_sink.

Drives a series of beats via cocotbext-axi's AxiStreamSource and confirms
the sink's beat_count and data_xor outputs match the expected values.
Kept simple — this is a stub block, not a complex DUT.
"""
from __future__ import annotations

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from cocotbext.axi import AxiStreamBus, AxiStreamSource

from pyuvm import uvm_test


CLK_PERIOD_NS = 10
AXIS_PREFIX = "s_axis"


async def _setup(dut) -> AxiStreamSource:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.s_axis_tvalid.value = 0
    dut.s_axis_tlast.value  = 0
    dut.s_axis_tdata.value  = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    return AxiStreamSource(
        AxiStreamBus.from_prefix(dut, AXIS_PREFIX),
        dut.clk,
        dut.rst_n,
        reset_active_level=False,
    )


@pyuvm.test()
class smoke(uvm_test):
    """Push a small fixed pattern and verify beat_count + data_xor."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.test_dut if hasattr(cocotb, "test_dut") else cocotb.top
            src = await _setup(dut)

            # A handful of beats with a known XOR.
            pattern = [0x00000001, 0x00000002, 0xDEADBEEF, 0x12345678,
                       0xA5A5A5A5, 0x5A5A5A5A]
            expected_xor = 0
            for w in pattern:
                expected_xor ^= w

            byte_lanes = len(dut.s_axis_tdata) // 8
            for w in pattern:
                await src.send(w.to_bytes(byte_lanes, "little"))
            await src.wait()
            # Give the sink a couple of cycles for the last beat to commit.
            await ClockCycles(dut.clk, 3)

            beat_count = int(dut.beat_count.value)
            data_xor   = int(dut.data_xor.value)
            self.logger.info(
                f"stream_sink: beat_count={beat_count} data_xor=0x{data_xor:08x}")
            assert beat_count == len(pattern), (
                f"beat_count mismatch: got {beat_count}, expected {len(pattern)}")
            assert data_xor == expected_xor, (
                f"data_xor mismatch: got 0x{data_xor:08x}, "
                f"expected 0x{expected_xor:08x}")
        finally:
            self.drop_objection()
