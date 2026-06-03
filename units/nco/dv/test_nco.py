"""Cocotb tests for nco.

Pattern A: bit-exact comparison against the Python reference.

The NCO is the first AXIS unit in the project where TDATA carries a
*complex* sample (I in low SAMPLE_W bits, Q in high SAMPLE_W bits).
The unpacking helper _split_iq() does that decoding.

Test scenarios:
* startup       — first sample after reset must be (cos(0), sin(0)) =
                   (max-positive, 0). Smoke check that LUT[0] is right
                   and the consumer-side handshake works.
* tone_slow     — phase_inc programmed for a slow tone, run for many
                   samples, confirm bit-exact match. Plot makes a
                   visible sinusoid.
* tone_fast     — phase_inc programmed for a faster tone (near
                   Nyquist). Same bit-exact check.
* freq_update   — programs slow then fast mid-stream. Python model
                   gets the same update; bit-exact agreement across
                   the boundary.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from cocotbext.axi import (
    AxiLiteBus, AxiLiteMaster,
    AxiStreamBus, AxiStreamSink,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import Nco  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

SAMPLE_W = int(os.environ.get("NCO_SAMPLE_W", "16"))
PHASE_W  = int(os.environ.get("NCO_PHASE_W",  "32"))
LUT_N    = int(os.environ.get("NCO_LUT_N",    "10"))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _split_iq(raw: int) -> tuple[int, int]:
    """Split a 2*SAMPLE_W wide payload into (I, Q) signed samples.
    Matches the RTL's TDATA packing: Q in high bits, I in low."""
    mask = (1 << SAMPLE_W) - 1
    i = _signed(raw & mask, SAMPLE_W)
    q = _signed((raw >> SAMPLE_W) & mask, SAMPLE_W)
    return (i, q)


def _plot_filename(testname: str) -> str:
    return f"nco__W{SAMPLE_W}_P{PHASE_W}_L{LUT_N}__{testname}"


def _plot(testname: str, iq_pairs, expected_pairs) -> None:
    # Plot the I (cos) component on top, the Q (sin) component below
    # using the existing 3-panel helper: input = expected I, output =
    # got I, diff = I diff. (Plot two graphs gives diagnostic for both
    # components but the helper is single-channel; pick the I trace
    # which is what most consumers care about. Tests still assert on
    # both components.)
    got_i  = [p[0] for p in iq_pairs]
    expt_i = [p[0] for p in expected_pairs]
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"nco {testname}: SAMPLE_W={SAMPLE_W}, "
               f"PHASE_W={PHASE_W}, LUT_N={LUT_N} (I/cos trace)"),
        inputs=expt_i,   # model trace also shown on the input panel
        expected=expt_i,
        got=got_i,
        output_label="cos output (model = reference)",
    )


async def _setup(dut) -> tuple[AxiLiteMaster, AxiStreamSink]:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.s_axil_awvalid.value = 0
    dut.s_axil_wvalid.value = 0
    dut.s_axil_bready.value = 0
    dut.s_axil_arvalid.value = 0
    dut.s_axil_rready.value = 0
    dut.m_axis_tready.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    axil = AxiLiteMaster(
        AxiLiteBus.from_prefix(dut, "s_axil"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return axil, sink


async def _program_phase_inc(axil: AxiLiteMaster, phase_inc: int) -> None:
    u = phase_inc & ((1 << 32) - 1)
    await axil.write(0, u.to_bytes(4, "little"))


async def _collect_iq(sink: AxiStreamSink, n: int) -> list[tuple[int, int]]:
    out = []
    byte_lanes = (2 * SAMPLE_W + 7) // 8
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        out.append(_split_iq(v))
    return out


def _build_model() -> Nco:
    return Nco(sample_w=SAMPLE_W, phase_w=PHASE_W, lut_n=LUT_N)


@pyuvm.test()
class startup(uvm_test):
    """Read the very first sample after reset; expect (max-pos, 0)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, sink = await _setup(dut)
            dut.m_axis_tready.value = 1
            # phase_inc=0 stays at phase=0 forever -> all samples
            # should be (max-pos, 0).
            await _program_phase_inc(axil, 0)

            model = _build_model()
            model.set_phase_inc(0)
            n = 4
            expected = model.run(n)
            got = await _collect_iq(sink, n)

            assert got == expected, (
                f"startup mismatch:\n  expected: {expected}\n  got:      {got}")
            self.logger.info(f"startup: {got[0]} (expected max-pos, 0)")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_slow(uvm_test):
    """Slow tone (~1/256 of sample rate). Lots of samples per cycle —
    smooth trace in the plot."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, sink = await _setup(dut)
            # NCO is free-running once tready goes high. Hold the sink
            # paused while programming phase_inc so it doesn't capture
            # beats produced before the desired frequency is set.
            sink.pause = True

            # Frequency = phase_inc / 2^PHASE_W. Pick 1/256.
            phase_inc = (1 << PHASE_W) // 256
            model = _build_model()
            model.set_phase_inc(phase_inc)
            await _program_phase_inc(axil, phase_inc)
            sink.pause = False

            n = 256
            expected = model.run(n)
            got = await _collect_iq(sink, n)

            _plot("tone_slow", got, expected)
            assert got == expected, (
                f"tone_slow mismatch ({n} samples); first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"tone_slow: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_fast(uvm_test):
    """Faster tone (1/8 of sample rate). Plot will be coarser; bit-
    exact match still required."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, sink = await _setup(dut)
            sink.pause = True

            phase_inc = (1 << PHASE_W) // 8
            model = _build_model()
            model.set_phase_inc(phase_inc)
            await _program_phase_inc(axil, phase_inc)
            sink.pause = False

            n = 64
            expected = model.run(n)
            got = await _collect_iq(sink, n)

            _plot("tone_fast", got, expected)
            assert got == expected, (
                f"tone_fast mismatch ({n} samples); first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"tone_fast: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_arbitrary(uvm_test):
    """Tone at a non-power-of-2 phase_inc. The power-of-2 phases used
    by tone_slow and tone_fast keep the LUT index advancing in
    power-of-2 steps, which leaves the lower index bits zero — bugs
    affecting only the lower LUT index bits slip past those tests. A
    non-power-of-2 advance exercises the full LUT index range with
    non-zero LSBs and catches such bugs."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, sink = await _setup(dut)
            sink.pause = True

            # 0xDEADBEEF: deliberately ugly bit pattern, top LUT_N bits
            # advance by an odd amount so every LUT index LSB sees
            # variation across the test.
            phase_inc = 0xDEADBEEF & ((1 << PHASE_W) - 1)
            model = _build_model()
            model.set_phase_inc(phase_inc)
            await _program_phase_inc(axil, phase_inc)
            sink.pause = False

            n = 128
            expected = model.run(n)
            got = await _collect_iq(sink, n)

            _plot("tone_arbitrary", got, expected)
            assert got == expected, (
                f"tone_arbitrary mismatch ({n} samples); first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"tone_arbitrary: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class freq_update(uvm_test):
    """Program slow tone, run, switch to fast tone mid-stream, run.
    Python model gets the same update; bit-exact agreement across the
    boundary."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, sink = await _setup(dut)
            sink.pause = True

            phase_a = (1 << PHASE_W) // 256
            phase_b = (1 << PHASE_W) // 16

            model = _build_model()
            model.set_phase_inc(phase_a)
            await _program_phase_inc(axil, phase_a)
            sink.pause = False

            n_a = 64
            expected_a = model.run(n_a)
            got_a = await _collect_iq(sink, n_a)

            # Mid-stream frequency change. Reset both DUT and model
            # phase so the second segment starts cleanly at phase=0
            # with the new phase_inc.
            #
            # Why reset instead of just rewriting phase_inc: the AXI-
            # Lite write to phase_inc takes some cycles to complete,
            # during which the NCO is still running at the old rate.
            # The Python model can't model that latency without
            # entangling itself with AXI-Lite timing. Resetting and
            # restarting gives both sides a clean reference frame.
            sink.pause = True
            dut.rst_n.value = 0
            await ClockCycles(dut.clk, 2)
            dut.rst_n.value = 1
            await ClockCycles(dut.clk, 2)
            # Drain any beats the sink captured before pause took
            # effect (cocotbext-axi sets tready combinationally; one
            # extra beat is possible).
            while not sink.empty():
                sink.recv_nowait()
            model = _build_model()           # fresh model = phase 0, regs at (LUT_SCALE, 0)
            model.set_phase_inc(phase_b)
            await _program_phase_inc(axil, phase_b)
            sink.pause = False

            n_b = 64
            expected_b = model.run(n_b)
            got_b = await _collect_iq(sink, n_b)

            expected = expected_a + expected_b
            got      = got_a      + got_b

            _plot("freq_update", got, expected)

            assert got == expected, (
                f"freq_update mismatch ({len(got)} samples); first diff "
                f"index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"freq_update: {n_a} slow + {n_b} fast, bit-exact across boundary")
        finally:
            self.drop_objection()
