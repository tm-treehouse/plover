"""Cocotb tests for cordic.

Pattern A: bit-exact comparison against the Python reference model.

Each input IQ sample produces one output beat (phase, magnitude),
with 16 cycles of pipeline latency. The model performs the same
math in a single Python call; bit-exact agreement is required.

Test scenarios:
* cardinal_points  — input the four cardinal axes; outputs should
                      hit exact phase values (0, +pi/2, +/-pi, -pi/2).
* slow_rotation    — drive a slowly-rotating tone; phase output
                      should ramp monotonically through 2*pi.
* random_samples   — pseudo-random IQ; bit-exact comparison.
* unity_circle     — large-amplitude inputs around the unit circle;
                      magnitude should stay roughly constant
                      (~Kn * amplitude).
"""
from __future__ import annotations

import math
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
from dv.dsp_models import Cordic  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

SAMPLE_W   = int(os.environ.get("CORDIC_SAMPLE_W",  "16"))
PHASE_W    = int(os.environ.get("CORDIC_PHASE_W",   "16"))
ITERATIONS = int(os.environ.get("CORDIC_ITERATIONS", "16"))
INTERNAL_W = SAMPLE_W + 2
# Output TDATA layout: { phase[PHASE_W-1:0], magnitude[INTERNAL_W-1:0] }
OUT_TDATA_W = PHASE_W + INTERNAL_W


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _pack_iq(i: int, q: int) -> int:
    mask = (1 << SAMPLE_W) - 1
    return ((q & mask) << SAMPLE_W) | (i & mask)


def _split_out(raw: int) -> tuple[int, int]:
    mag_mask   = (1 << INTERNAL_W) - 1
    phase_mask = (1 << PHASE_W) - 1
    mag   = _signed(raw & mag_mask, INTERNAL_W)
    phase = _signed((raw >> INTERNAL_W) & phase_mask, PHASE_W)
    return (mag, phase)


def _plot_filename(testname: str) -> str:
    return f"cordic__W{SAMPLE_W}_P{PHASE_W}__{testname}"


def _plot(testname: str, got, expected, inputs, kind: str = "phase") -> None:
    """Plot either the phase trace or the magnitude trace."""
    idx = 1 if kind == "phase" else 0
    got_v  = [p[idx] for p in got]
    expt_v = [p[idx] for p in expected]
    in_i   = [p[0] for p in inputs]
    plot_test_result(
        filename=_plot_filename(testname),
        title=f"cordic {testname}: SAMPLE_W={SAMPLE_W}, PHASE_W={PHASE_W} ({kind} trace)",
        inputs=in_i,
        expected=expt_v,
        got=got_v,
        output_label=f"{kind} out (model = reference)",
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


async def _drive_iq(source: AxiStreamSource, samples: list[tuple[int, int]]) -> None:
    byte_lanes = (2 * SAMPLE_W + 7) // 8
    for (i, q) in samples:
        raw = _pack_iq(i, q)
        await source.send(AxiStreamFrame(raw.to_bytes(byte_lanes, "little")))


async def _collect_out(sink: AxiStreamSink, n: int) -> list[tuple[int, int]]:
    byte_lanes = (OUT_TDATA_W + 7) // 8
    out = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        # Slice to the real bit width.
        v &= (1 << OUT_TDATA_W) - 1
        out.append(_split_out(v))
    return out


def _build_model() -> Cordic:
    return Cordic(sample_w=SAMPLE_W, phase_w=PHASE_W, iterations=ITERATIONS)


@pyuvm.test()
class cardinal_points(uvm_test):
    """Inputs along the four cardinal axes. Phase outputs should be
    exactly 0, +pi/2, +/-pi, -pi/2 (in the Q-encoding)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Each cardinal direction at half-scale to avoid edge cases
            # at +max/-min.
            amp = 1 << (SAMPLE_W - 2)
            samples = [
                ( amp,    0),    # +x: phase = 0
                (    0,  amp),   # +y: phase = +pi/2
                (-amp,    0),    # -x: phase = +/-pi
                (    0, -amp),   # -y: phase = -pi/2
            ] * 4   # repeat for stability

            model = _build_model()
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_out(sink, len(samples))

            _plot("cardinal_points", got, expected, samples, kind="phase")
            assert got == expected, (
                f"cardinal_points mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"cardinal_points: {len(samples)} samples, bit-exact. "
                f"First 4 outputs: {got[:4]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class slow_rotation(uvm_test):
    """A slowly-rotating IQ signal at constant amplitude. Output phase
    should ramp monotonically through 2*pi (modulo wrap)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 128
            amp = 1 << (SAMPLE_W - 2)
            samples = [
                (int(amp * math.cos(2 * math.pi * k / 64)),
                 int(amp * math.sin(2 * math.pi * k / 64)))
                for k in range(n)
            ]

            model = _build_model()
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_out(sink, n)

            _plot("slow_rotation", got, expected, samples, kind="phase")
            assert got == expected, (
                f"slow_rotation mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"slow_rotation: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_samples(uvm_test):
    """Pseudo-random IQ. Covers all four quadrants and arbitrary
    magnitudes — exercises the pre-rotation logic and every shift-add
    sign combination."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            rng = random.Random(0x123CAFE)
            lim = 1 << (SAMPLE_W - 2)
            n = 128
            samples = [(rng.randint(-lim, lim - 1),
                        rng.randint(-lim, lim - 1)) for _ in range(n)]

            model = _build_model()
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_out(sink, n)

            _plot("random_samples", got, expected, samples, kind="phase")
            assert got == expected, (
                f"random_samples mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"random_samples: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class unity_circle(uvm_test):
    """Inputs around the unit circle at large amplitude. Magnitude
    output should stay approximately constant (~Kn * amplitude).
    Verifies the magnitude path bit-exactly against the model."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 64
            amp = (1 << (SAMPLE_W - 1)) - 1   # near full-scale
            samples = [
                (int(amp * math.cos(2 * math.pi * k / n)),
                 int(amp * math.sin(2 * math.pi * k / n)))
                for k in range(n)
            ]

            model = _build_model()
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_out(sink, n)

            _plot("unity_circle", got, expected, samples, kind="magnitude")
            assert got == expected, (
                f"unity_circle mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            mags = [p[0] for p in got]
            self.logger.info(
                f"unity_circle: {n} samples, bit-exact. "
                f"Magnitude range: {min(mags)}..{max(mags)} "
                f"(Kn * amp = {int(1.6468 * amp)})")
        finally:
            self.drop_objection()
