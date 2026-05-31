"""Cocotb tests for cic_decimator.

Pattern A: direct cocotb tests, numeric reference is the bit-exact
:class:`CicDecimator` Python model from ``dv/dsp_models.py``. No agent /
env / scoreboard ceremony — just push samples in via cocotbext-axi,
collect samples out, compare to the model.

The DSP "doesn't fit the UVM mould" claim from earlier sessions, made
concrete: each test reads as ten lines of intent (drive these samples,
expect these), not a hundred lines of sequence/item/driver/monitor wiring.
The reference model is the spec; the test asserts hardware == spec
sample-for-sample.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from cocotbext.axi import (
    AxiStreamBus, AxiStreamSource, AxiStreamSink,
)

from pyuvm import uvm_test

# Resolve the project root so we can import the shared reference models.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import CicDecimator  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

# Parameters used by the test. Must match what the harness passes at
# build time so the Python reference matches the RTL exactly.
STAGES = int(os.environ.get("CIC_STAGES", "3"))
DECIM  = int(os.environ.get("CIC_DECIM",  "4"))
DELAY  = int(os.environ.get("CIC_DELAY",  "1"))
IN_W   = int(os.environ.get("CIC_IN_W",   "16"))
OUT_W  = int(os.environ.get("CIC_OUT_W",  "16"))


def _plot_filename(testname: str) -> str:
    """File-system-friendly slug embedding the parameter config so each
    parametrised run produces a distinct PNG."""
    return f"cic_decimator__N{STAGES}_R{DECIM}_M{DELAY}_W{IN_W}-{OUT_W}__{testname}"


def _plot(testname: str, inputs, expected, got) -> None:
    """Helper: produce comparison PNG. Called even when assertions
    failed (so the diff plot can be inspected). Robust to length
    mismatches between got/expected."""
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"cic_decimator {testname}: "
               f"N={STAGES}, R={DECIM}, M={DELAY}, "
               f"IN_W={IN_W}, OUT_W={OUT_W}"),
        inputs=inputs,
        expected=expected,
        got=got,
        input_rate_ratio=1.0 / DECIM,
        output_label="output (decimated)",
    )


def _signed(value: int, width: int) -> int:
    """Bytes-to-signed-int helper used to interpret AXIS payload bytes."""
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


async def _setup(dut) -> tuple[AxiStreamSource, AxiStreamSink]:
    """Bring up clock, reset, and the AXIS BFMs."""
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    # Idle inputs through reset.
    dut.s_axis_tvalid.value = 0
    dut.m_axis_tready.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    src = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return src, sink


async def _send_samples(src: AxiStreamSource, samples: list[int]) -> None:
    """Send a list of signed IN_W-bit samples as single-beat AXIS frames."""
    byte_lanes = (IN_W + 7) // 8
    for s in samples:
        # Convert signed Python int to little-endian unsigned bytes.
        v = s & ((1 << (byte_lanes * 8)) - 1)
        await src.send(v.to_bytes(byte_lanes, "little"))


async def _collect_samples(sink: AxiStreamSink, n: int) -> list[int]:
    """Read n output samples (one per AXIS frame) as signed OUT_W-bit ints."""
    out = []
    for _ in range(n):
        frame = await sink.recv()
        # cocotbext-axi returns the frame; .tdata is the bytes payload of
        # however many beats were in the frame. For our single-beat
        # output frames the payload is one OUT_W-sized lane.
        v = int.from_bytes(frame.tdata, "little")
        out.append(_signed(v, OUT_W))
    return out


@pyuvm.test()
class impulse(uvm_test):
    """Impulse response. The reference model is the spec; hardware must
    match it sample-for-sample."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            # Start consuming output right away.
            dut.m_axis_tready.value = 1

            # Reference model with the same params.
            model = CicDecimator(stages=STAGES, decim=DECIM, delay=DELAY,
                                 in_w=IN_W, out_w=OUT_W)

            # Generous-length impulse: one nonzero sample, then many zeros
            # so we can see the comb's transient ringing decay.
            n_inputs = DECIM * 32
            inputs = [0] * n_inputs
            inputs[0] = (1 << (IN_W - 2))  # 0.5 in Q1.(IN_W-1)

            # Compute expected stream from the model.
            expected = model.run(inputs)

            # Drive RTL and collect.
            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            # Plot before asserting — diagnostic on failure, sanity-check on pass.
            _plot("impulse", inputs, expected, got)

            assert got == expected, (
                f"impulse mismatch:\n"
                f"  expected (first 8): {expected[:8]}\n"
                f"  got      (first 8): {got[:8]}\n"
                f"  first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}"
            )
            self.logger.info(
                f"impulse: {len(got)} output samples, bit-exact match")
        finally:
            self.drop_objection()


@pyuvm.test()
class step(uvm_test):
    """Step response."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = CicDecimator(stages=STAGES, decim=DECIM, delay=DELAY,
                                 in_w=IN_W, out_w=OUT_W)
            n_inputs = DECIM * 32
            level = 1 << (IN_W - 3)  # 0.25 in Q1.(IN_W-1)
            inputs = [level] * n_inputs
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("step", inputs, expected, got)

            assert got == expected, (
                f"step mismatch:\n  expected: {expected[:8]}...\n"
                f"  got:      {got[:8]}...")
            # CIC step response settles; final samples should be constant.
            assert got[-3:] == expected[-3:], "settled-state mismatch"
            self.logger.info(f"step: {len(got)} samples, bit-exact, "
                              f"settles at {got[-1]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_pattern(uvm_test):
    """Random samples — bit-exactness shouldn't depend on input shape."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = CicDecimator(stages=STAGES, decim=DECIM, delay=DELAY,
                                 in_w=IN_W, out_w=OUT_W)
            rng = random.Random(0xCAFE_F00D)
            n_inputs = DECIM * 50
            # Use the full IN_W signed range.
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1
            inputs = [rng.randint(lo, hi) for _ in range(n_inputs)]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("random_pattern", inputs, expected, got)

            assert got == expected, (
                f"random mismatch ({len(got)} samples):\n"
                f"  first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"random_pattern: {len(got)} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class backpressure(uvm_test):
    """Output is bit-exact even when the consumer holds tready low
    intermittently. The unit should stall input cleanly under sustained
    backpressure rather than dropping samples."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)

            # Pulse tready: hold high for 5 cycles, low for 5 cycles,
            # repeating. The unit must produce every sample the model
            # predicts; AXIS backpressure must not cause sample loss.
            async def pulse_tready():
                while True:
                    dut.m_axis_tready.value = 1
                    await ClockCycles(dut.clk, 5)
                    dut.m_axis_tready.value = 0
                    await ClockCycles(dut.clk, 5)
            cocotb.start_soon(pulse_tready())

            model = CicDecimator(stages=STAGES, decim=DECIM, delay=DELAY,
                                 in_w=IN_W, out_w=OUT_W)
            rng = random.Random(42)
            n_inputs = DECIM * 30
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1
            inputs = [rng.randint(lo, hi) for _ in range(n_inputs)]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("backpressure", inputs, expected, got)

            assert got == expected, (
                f"backpressure mismatch ({len(got)} samples expected, "
                f"got {len(got)}); first diff: "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"backpressure: {len(got)} samples bit-exact under intermittent tready")
        finally:
            self.drop_objection()
