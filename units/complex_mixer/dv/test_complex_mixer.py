"""Cocotb tests for complex_mixer.

Pattern A: bit-exact comparison against the Python reference.

The mixer takes two synchronous AXIS-IQ streams and produces one
AXIS-IQ output per pair. Each TDATA is 2*SAMPLE_W bits with Q in the
high bits and I in the low bits — same convention as the NCO.

Test scenarios:
* unity_passthrough — a = stream, b = (1+j0). Output should equal a
                       (within the Q-position-preserving truncation).
* j_rotation       — a = stream, b = (0+j1). Output should be jot rotated
                       90 degrees (out.I = -a.Q, out.Q = a.I).
* random_streams   — both inputs random. Bit-exact match required.
* dc_x_tone        — a = constant DC, b = sinusoid. Output should be
                       the sinusoid scaled by the DC value.
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
from dv.dsp_models import ComplexMixer  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

SAMPLE_W      = int(os.environ.get("MIX_SAMPLE_W",       "16"))
SAMPLE_INT_W  = int(os.environ.get("MIX_SAMPLE_INT_W",   "1"))
SAMPLE_FRAC_W = int(os.environ.get("MIX_SAMPLE_FRAC_W",  str(SAMPLE_W - 1)))
OUT_SHIFT     = int(os.environ.get("MIX_OUT_SHIFT",      str(SAMPLE_FRAC_W)))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _pack_iq(i: int, q: int) -> int:
    """Pack (I, Q) into the TDATA layout: Q in high bits, I in low."""
    mask = (1 << SAMPLE_W) - 1
    return ((q & mask) << SAMPLE_W) | (i & mask)


def _split_iq(raw: int) -> tuple[int, int]:
    mask = (1 << SAMPLE_W) - 1
    return (_signed(raw & mask, SAMPLE_W),
            _signed((raw >> SAMPLE_W) & mask, SAMPLE_W))


def _plot_filename(testname: str) -> str:
    return f"complex_mixer__W{SAMPLE_W}_S{OUT_SHIFT}__{testname}"


def _plot(testname: str, iq_pairs, expected_pairs, a_samples) -> None:
    """Single-trace plot of the I component (output and reference)."""
    got_i  = [p[0] for p in iq_pairs]
    expt_i = [p[0] for p in expected_pairs]
    a_i    = [p[0] for p in a_samples]
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"complex_mixer {testname}: SAMPLE_W={SAMPLE_W}, "
               f"OUT_SHIFT={OUT_SHIFT} (I trace)"),
        inputs=a_i,
        expected=expt_i,
        got=got_i,
        output_label="I component (model = reference)",
    )


async def _setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.s_axis_a_tvalid.value = 0
    dut.s_axis_b_tvalid.value = 0
    dut.m_axis_tready.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    source_a = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis_a"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    source_b = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis_b"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return source_a, source_b, sink


async def _drive_iq(source: AxiStreamSource, samples: list[tuple[int, int]]) -> None:
    """Send a list of (I, Q) pairs as AXIS beats."""
    byte_lanes = (2 * SAMPLE_W + 7) // 8
    for (i, q) in samples:
        raw = _pack_iq(i, q)
        await source.send(AxiStreamFrame(raw.to_bytes(byte_lanes, "little")))


async def _collect_iq(sink: AxiStreamSink, n: int) -> list[tuple[int, int]]:
    byte_lanes = (2 * SAMPLE_W + 7) // 8
    out = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        out.append(_split_iq(v))
    return out


def _build_model() -> ComplexMixer:
    return ComplexMixer(
        sample_w=SAMPLE_W,
        sample_int_w=SAMPLE_INT_W,
        sample_frac_w=SAMPLE_FRAC_W,
        out_shift=OUT_SHIFT,
    )


@pyuvm.test()
class unity_passthrough(uvm_test):
    """b = (max-pos, 0) (~unit real). Output should equal a after
    truncation."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source_a, source_b, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 32
            unity = (1 << (SAMPLE_W - 1)) - 1
            a_samples = [(_signed(i * 257, SAMPLE_W), _signed(-i * 257, SAMPLE_W))
                         for i in range(n)]
            b_samples = [(unity, 0)] * n

            model = _build_model()
            expected = model.run(a_samples, b_samples)

            # Drive both inputs concurrently.
            cocotb.start_soon(_drive_iq(source_a, a_samples))
            cocotb.start_soon(_drive_iq(source_b, b_samples))

            got = await _collect_iq(sink, n)
            _plot("unity_passthrough", got, expected, a_samples)
            assert got == expected, (
                f"unity_passthrough mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"unity_passthrough: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class j_rotation(uvm_test):
    """b = (0, max-pos) (~j). Output should be a rotated by 90 deg:
    out.I = -a.Q, out.Q = a.I (within truncation)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source_a, source_b, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 32
            unity = (1 << (SAMPLE_W - 1)) - 1
            a_samples = [(_signed(i * 257, SAMPLE_W), _signed((i + 1) * 333, SAMPLE_W))
                         for i in range(n)]
            b_samples = [(0, unity)] * n

            model = _build_model()
            expected = model.run(a_samples, b_samples)

            cocotb.start_soon(_drive_iq(source_a, a_samples))
            cocotb.start_soon(_drive_iq(source_b, b_samples))

            got = await _collect_iq(sink, n)
            _plot("j_rotation", got, expected, a_samples)
            assert got == expected, (
                f"j_rotation mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"j_rotation: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_streams(uvm_test):
    """Both inputs pseudo-random. Exercises every multiplier path with
    non-trivial sign combinations."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            source_a, source_b, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            rng = random.Random(0xC0FFEE)
            lim = 1 << (SAMPLE_W - 1)
            n = 64
            a_samples = [(rng.randint(-lim, lim - 1), rng.randint(-lim, lim - 1))
                         for _ in range(n)]
            b_samples = [(rng.randint(-lim, lim - 1), rng.randint(-lim, lim - 1))
                         for _ in range(n)]

            model = _build_model()
            expected = model.run(a_samples, b_samples)

            cocotb.start_soon(_drive_iq(source_a, a_samples))
            cocotb.start_soon(_drive_iq(source_b, b_samples))

            got = await _collect_iq(sink, n)
            _plot("random_streams", got, expected, a_samples)
            assert got == expected, (
                f"random_streams mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"random_streams: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class dc_x_tone(uvm_test):
    """a = constant DC (purely real); b = a synthesised tone. Output
    should be b scaled by a's DC value — i.e. the same tone with
    smaller amplitude."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            import math
            dut = cocotb.top
            source_a, source_b, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            n = 64
            dc = (1 << (SAMPLE_W - 2))  # 0.5 in Q1.15
            amp = (1 << (SAMPLE_W - 1)) - 1
            a_samples = [(dc, 0)] * n
            b_samples = [(int(amp * math.cos(2 * math.pi * k / 16)),
                          int(amp * math.sin(2 * math.pi * k / 16)))
                         for k in range(n)]

            model = _build_model()
            expected = model.run(a_samples, b_samples)

            cocotb.start_soon(_drive_iq(source_a, a_samples))
            cocotb.start_soon(_drive_iq(source_b, b_samples))

            got = await _collect_iq(sink, n)
            _plot("dc_x_tone", got, expected, a_samples)
            assert got == expected, (
                f"dc_x_tone mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"dc_x_tone: {n} samples, bit-exact")
        finally:
            self.drop_objection()
