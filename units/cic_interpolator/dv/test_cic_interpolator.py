"""Cocotb tests for cic_interpolator.

Same Pattern A approach as cic_decimator: bit-exact comparison against
the shared Python reference model. The interpolator is structurally
the reverse of the decimator — combs run at the low input rate, then
zero-stuff upsample, then integrators at the high output rate.

For each input sample, we expect exactly R output samples (one full
"burst" per input).
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import CicInterpolator  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

STAGES = int(os.environ.get("CIC_STAGES", "3"))
INTERP = int(os.environ.get("CIC_INTERP", "4"))
DELAY  = int(os.environ.get("CIC_DELAY",  "1"))
IN_W   = int(os.environ.get("CIC_IN_W",   "16"))
OUT_W  = int(os.environ.get("CIC_OUT_W",  "16"))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return f"cic_interpolator__N{STAGES}_R{INTERP}_M{DELAY}_W{IN_W}-{OUT_W}__{testname}"


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"cic_interpolator {testname}: "
               f"N={STAGES}, R={INTERP}, M={DELAY}, "
               f"IN_W={IN_W}, OUT_W={OUT_W}"),
        inputs=inputs,
        expected=expected,
        got=got,
        # Input rate < output rate by factor INTERP; spread the input
        # x-axis by INTERP so it lines up visually with the outputs.
        input_rate_ratio=INTERP,
        output_label="output (interpolated)",
    )


async def _setup(dut) -> tuple[AxiStreamSource, AxiStreamSink]:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
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
    byte_lanes = (IN_W + 7) // 8
    for s in samples:
        v = s & ((1 << (byte_lanes * 8)) - 1)
        await src.send(v.to_bytes(byte_lanes, "little"))


async def _collect_samples(sink: AxiStreamSink, n: int) -> list[int]:
    out = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        out.append(_signed(v, OUT_W))
    return out


@pyuvm.test()
class impulse(uvm_test):
    """Impulse: one input sample, INTERP output samples = the unit's
    impulse response over one burst."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = CicInterpolator(stages=STAGES, interp=INTERP,
                                    delay=DELAY, in_w=IN_W, out_w=OUT_W)
            n_inputs = 16
            inputs = [0] * n_inputs
            inputs[0] = (1 << (IN_W - 2))  # 0.5 in Q1.(IN_W-1)
            expected = model.run(inputs)  # length = INTERP * n_inputs

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("impulse", inputs, expected, got)

            assert got == expected, (
                f"impulse mismatch:\n"
                f"  expected (first 12): {expected[:12]}\n"
                f"  got      (first 12): {got[:12]}\n"
                f"  first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"impulse: {len(got)} output samples ({INTERP}x{n_inputs}), bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class step(uvm_test):
    """Step input — interpolator should produce a smooth-ish ramp at
    the output rate."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = CicInterpolator(stages=STAGES, interp=INTERP,
                                    delay=DELAY, in_w=IN_W, out_w=OUT_W)
            n_inputs = 16
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
            self.logger.info(f"step: {len(got)} samples, bit-exact, "
                              f"settles at {got[-1]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_pattern(uvm_test):
    """Random samples."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = CicInterpolator(stages=STAGES, interp=INTERP,
                                    delay=DELAY, in_w=IN_W, out_w=OUT_W)
            rng = random.Random(0xCAFE_F00D)
            n_inputs = 24
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
    """Output bit-exact under intermittent consumer backpressure."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            src, sink = await _setup(dut)

            async def pulse_tready():
                while True:
                    dut.m_axis_tready.value = 1
                    await ClockCycles(dut.clk, 5)
                    dut.m_axis_tready.value = 0
                    await ClockCycles(dut.clk, 5)
            cocotb.start_soon(pulse_tready())

            model = CicInterpolator(stages=STAGES, interp=INTERP,
                                    delay=DELAY, in_w=IN_W, out_w=OUT_W)
            rng = random.Random(42)
            n_inputs = 16
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1
            inputs = [rng.randint(lo, hi) for _ in range(n_inputs)]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("backpressure", inputs, expected, got)

            assert got == expected, (
                f"backpressure mismatch ({len(got)} samples); first diff: "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"backpressure: {len(got)} samples bit-exact under intermittent tready")
        finally:
            self.drop_objection()
