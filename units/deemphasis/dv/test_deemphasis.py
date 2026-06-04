"""Cocotb tests for deemphasis.

Pattern A: bit-exact comparison against the Python reference. The
de-emphasis filter is a single-pole IIR low-pass. Like the DC blocker
(same architectural shape), the feedback path means an arithmetic
disagreement compounds quickly, so the bit-exact comparison is
particularly load-bearing.

Test scenarios:
* dc_settle     — programs alpha, drives a DC step. Output should
                   approach the input value (unity DC gain) within
                   the documented truncation drift.
* impulse       — drives a single nonzero sample. Output is the IIR
                   impulse response: (1-alpha)*x then exponential
                   decay by factor alpha.
* tone_in_band  — drives a sinusoid below the corner frequency. Should
                   pass through with mild attenuation.
* tone_above    — drives a sinusoid above the corner. Should be more
                   strongly attenuated. Bit-exact comparison covers
                   both cases.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import cocotb
import pyuvm
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from cocotbext.axi import (
    AxiLiteBus, AxiLiteMaster,
    AxiStreamBus, AxiStreamSource, AxiStreamSink,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import Deemphasis  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

SAMPLE_W    = int(os.environ.get("DEMPH_SAMPLE_W",    "16"))
COEF_W      = int(os.environ.get("DEMPH_COEF_W",      "16"))
COEF_INT_W  = int(os.environ.get("DEMPH_COEF_INT_W",  "1"))
COEF_FRAC_W = int(os.environ.get("DEMPH_COEF_FRAC_W", str(COEF_W - 1)))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return f"deemphasis__W{SAMPLE_W}_C{COEF_W}__{testname}"


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=f"deemphasis {testname}: SAMPLE_W={SAMPLE_W}, COEF_W={COEF_W}",
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="de-emphasis output (model = reference)",
    )


async def _setup(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())
    dut.s_axil_awvalid.value = 0
    dut.s_axil_wvalid.value = 0
    dut.s_axil_bready.value = 0
    dut.s_axil_arvalid.value = 0
    dut.s_axil_rready.value = 0
    dut.s_axis_tvalid.value = 0
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
    src = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return axil, src, sink


async def _program_alpha(axil: AxiLiteMaster, alpha: int) -> None:
    u = alpha & ((1 << 32) - 1)
    await axil.write(0, u.to_bytes(4, "little"))


async def _send_samples(src: AxiStreamSource, samples: list[int]) -> None:
    byte_lanes = (SAMPLE_W + 7) // 8
    for s in samples:
        v = s & ((1 << (byte_lanes * 8)) - 1)
        await src.send(v.to_bytes(byte_lanes, "little"))


async def _collect_samples(sink: AxiStreamSink, n: int) -> list[int]:
    byte_lanes = (SAMPLE_W + 7) // 8
    out = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        v &= (1 << SAMPLE_W) - 1
        out.append(_signed(v, SAMPLE_W))
    return out


def _build_model() -> Deemphasis:
    return Deemphasis(sample_w=SAMPLE_W, coef_w=COEF_W,
                      coef_int_w=COEF_INT_W, coef_frac_w=COEF_FRAC_W)


async def _setup_and_program(dut, alpha: int):
    """Wires up the BFMs and programs alpha into both the RTL and the
    Python model in lock-step. Returns (src, sink, model)."""
    axil, src, sink = await _setup(dut)
    dut.m_axis_tready.value = 1
    await _program_alpha(axil, alpha)
    model = _build_model()
    model.set_alpha(alpha)
    return src, sink, model


@pyuvm.test()
class dc_settle(uvm_test):
    """DC step input. Output should approach the input level."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            # Strong alpha (~0.9) for a slow, visible settling curve.
            alpha = int(round(0.9 * (1 << COEF_FRAC_W)))
            src, sink, model = await _setup_and_program(dut, alpha)

            n = 200
            samples = [12000] * n
            expected = model.run(samples)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n)

            _plot("dc_settle", samples, expected, got)
            assert got == expected, (
                f"dc_settle mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"dc_settle: {n} samples, bit-exact. final out={got[-1]} "
                f"(input 12000, alpha~0.9)")
        finally:
            self.drop_objection()


@pyuvm.test()
class impulse(uvm_test):
    """Single impulse input. Output is the IIR impulse response."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            # Use a sharper alpha (~0.5) so the decay is visible in
            # only a few samples — quick to inspect on the plot.
            alpha = int(round(0.5 * (1 << COEF_FRAC_W)))
            src, sink, model = await _setup_and_program(dut, alpha)

            n = 32
            samples = [30000] + [0] * (n - 1)
            expected = model.run(samples)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n)

            _plot("impulse", samples, expected, got)
            assert got == expected, (
                f"impulse mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            # First output is (1-alpha)*30000 = 0.5*30000 = 15000.
            # Each subsequent output is alpha * prev = 0.5 * prev.
            self.logger.info(
                f"impulse: {n} samples, bit-exact. "
                f"impulse response first 4: {got[:4]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_in_band(uvm_test):
    """Sinusoid below the corner frequency. Mild attenuation expected."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            # alpha = 0.7574 (~ 48kHz / 75us de-emphasis), scaled by Q.
            alpha = int(round(0.7574 * (1 << COEF_FRAC_W)))
            src, sink, model = await _setup_and_program(dut, alpha)

            n = 128
            amp = 1 << (SAMPLE_W - 2)
            # freq_norm = 0.02 cycles/sample — well below the corner
            # at this alpha (corner ~ -ln(alpha)/(2*pi) ~ 0.044).
            samples = [int(amp * math.sin(2 * math.pi * 0.02 * k))
                       for k in range(n)]
            expected = model.run(samples)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n)

            _plot("tone_in_band", samples, expected, got)
            assert got == expected, (
                f"tone_in_band mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"tone_in_band: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_above(uvm_test):
    """Sinusoid above the corner frequency. Stronger attenuation."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            # 0.7574 ~ exp(-1/(48000 * 75e-6)) — the standard 48 kHz/75 us
            # de-emphasis pole. Scaled to whatever COEF_FRAC_W is.
            alpha = int(round(0.7574 * (1 << COEF_FRAC_W)))
            src, sink, model = await _setup_and_program(dut, alpha)

            n = 128
            amp = 1 << (SAMPLE_W - 2)
            # freq_norm = 0.15 — well above the corner frequency.
            samples = [int(amp * math.sin(2 * math.pi * 0.15 * k))
                       for k in range(n)]
            expected = model.run(samples)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n)

            _plot("tone_above", samples, expected, got)
            assert got == expected, (
                f"tone_above mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            # Sanity: peak amplitude should be smaller than input peak.
            in_peak = max(samples) - min(samples)
            out_peak = max(got) - min(got)
            ratio = out_peak / in_peak
            self.logger.info(
                f"tone_above: {n} samples, bit-exact. "
                f"output peak-to-peak / input = {ratio:.3f} "
                f"(less than 1.0 due to LPF)")
            assert ratio < 0.6, (
                f"tone_above: high freq should be attenuated; "
                f"got ratio {ratio:.3f} (expected < 0.6)")
        finally:
            self.drop_objection()
