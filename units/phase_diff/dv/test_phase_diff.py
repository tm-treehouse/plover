"""Cocotb tests for phase_diff.

Pattern A: bit-exact comparison against the Python reference.

The unit subtracts consecutive phase samples in signed-modular
PHASE_W-bit arithmetic. The implicit wrap is the whole point — when
phase crosses +-pi (i.e. PHASE_W-bit signed extremes), the
subtractor wraps naturally to a small physical-frequency step
rather than a near-2*pi jump.

Test scenarios:
* linear_ramp     — constant-frequency input (linear phase ramp).
                     Output should be a constant (after the first
                     bogus beat).
* phase_wrap      — phase samples that deliberately cross +-pi.
                     Wrap handling should produce small frequency
                     steps, not 2*pi-sized jumps.
* random_phases   — pseudo-random phases.  Bit-exact across the
                     full range.
* constant_phase  — phase doesn't change.  Output frequency is 0
                     (after the first beat).
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
    AxiStreamBus, AxiStreamSource, AxiStreamSink, AxiStreamFrame,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import PhaseDiff  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

PHASE_W = int(os.environ.get("PD_PHASE_W", "16"))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return f"phase_diff__P{PHASE_W}__{testname}"


def _plot(testname: str, got, expected, inputs) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=f"phase_diff {testname}: PHASE_W={PHASE_W}",
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="frequency (model = reference)",
    )


async def _setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.s_axis_tvalid.value = 0
    dut.m_axis_tready.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    source = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return source, sink


async def _drive(source: AxiStreamSource, samples: list[int]) -> None:
    """Send PHASE_W-bit signed samples as AXIS beats. The BFM expects
    byte-multiple widths; PHASE_W=16 happens to be byte-aligned. For
    other widths the test would need padding."""
    byte_lanes = (PHASE_W + 7) // 8
    mask = (1 << PHASE_W) - 1
    for v in samples:
        await source.send(AxiStreamFrame(
            (v & mask).to_bytes(byte_lanes, "little")))


async def _collect(sink: AxiStreamSink, n: int) -> list[int]:
    byte_lanes = (PHASE_W + 7) // 8
    out = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        v &= (1 << PHASE_W) - 1
        out.append(_signed(v, PHASE_W))
    return out


@pyuvm.test()
class linear_ramp(uvm_test):
    """Constant-frequency input: phase ramps linearly. Output frequency
    should be constant (= ramp step), modulo the first bogus beat."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 64
            step = 800
            # Start mid-range so the ramp doesn't immediately wrap.
            samples = [_signed((k * step) & ((1 << PHASE_W) - 1), PHASE_W)
                       for k in range(n)]
            model = PhaseDiff(phase_w=PHASE_W)
            expected = model.run(samples)

            cocotb.start_soon(_drive(source, samples))
            got = await _collect(sink, n)

            _plot("linear_ramp", got, expected, samples)
            assert got == expected, (
                f"linear_ramp mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"linear_ramp: {n} samples, bit-exact. Steady-state freq = {got[5]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class phase_wrap(uvm_test):
    """Phase samples that cross +/-pi. The subtractor's bit-width wrap
    should produce small +ve frequency steps across the boundary
    rather than near-2*pi jumps."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Constant +500 step in phase, walking through several
            # wraps. With PHASE_W=16, one full cycle = 65536 samples
            # of step 1; here we use step 500 so a full cycle takes
            # ~131 samples. Drive 200 samples so we see ~1.5 wraps.
            step = 500
            n = 200
            mask = (1 << PHASE_W) - 1
            samples = []
            phase = 0
            for _ in range(n):
                samples.append(_signed(phase & mask, PHASE_W))
                phase += step
            model = PhaseDiff(phase_w=PHASE_W)
            expected = model.run(samples)

            cocotb.start_soon(_drive(source, samples))
            got = await _collect(sink, n)

            _plot("phase_wrap", got, expected, samples)
            assert got == expected, (
                f"phase_wrap mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            # Sanity: after the first beat, every freq should equal `step`.
            assert all(f == step for f in got[1:]), (
                f"phase_wrap: post-first-beat freqs should all equal {step}; "
                f"got distinct values {sorted(set(got[1:]))[:5]}...")
            self.logger.info(
                f"phase_wrap: {n} samples, bit-exact, post-first-beat freq = {step} as expected")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_phases(uvm_test):
    """Pseudo-random phases covering the full PHASE_W signed range."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            rng = random.Random(0xF00DBEEF)
            lim = 1 << (PHASE_W - 1)
            n = 128
            samples = [rng.randint(-lim, lim - 1) for _ in range(n)]
            model = PhaseDiff(phase_w=PHASE_W)
            expected = model.run(samples)

            cocotb.start_soon(_drive(source, samples))
            got = await _collect(sink, n)

            _plot("random_phases", got, expected, samples)
            assert got == expected, (
                f"random_phases mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"random_phases: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class constant_phase(uvm_test):
    """Phase doesn't change. Output frequency = 0 (after first beat)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 32
            samples = [12345] * n
            model = PhaseDiff(phase_w=PHASE_W)
            expected = model.run(samples)

            cocotb.start_soon(_drive(source, samples))
            got = await _collect(sink, n)

            _plot("constant_phase", got, expected, samples)
            assert got == expected, (
                f"constant_phase mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            assert all(f == 0 for f in got[1:]), (
                f"constant_phase: all post-first freqs should be 0; "
                f"got distinct values {sorted(set(got[1:]))[:5]}")
            self.logger.info(f"constant_phase: {n} samples, bit-exact, all freq=0 after first")
        finally:
            self.drop_objection()
