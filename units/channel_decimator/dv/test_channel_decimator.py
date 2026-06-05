"""Cocotb tests for channel_decimator.

Same Pattern A bit-exact methodology as audio_decimator: drive an input,
compare against CicFirChain configured with matching parameters. The
wrapper has no arithmetic of its own — composition bit-exactness flows
from the per-unit bit-exactness of CIC and FIR.

This unit is the channel-rate decimator for the FM receive chain
front-end (~10x decimation: wide baseband -> intermediate chain rate).
Sibling of audio_decimator which targets the back-end 5x audio step.

Test scenarios (same shape as audio_decimator):
* impulse        — delta-coef FIR + impulse input. Output is the
                    CIC's impulse response.
* dc_step        — sustained DC input + uniform-coef FIR. Output
                    settles to ~DC level.
* tone_passband  — low-frequency tone, passes through.
* tone_stopband  — tone at the CIC's first null (f = 1/DECIM).
                    Heavily attenuated, but the residual is
                    bit-exact between model and RTL.
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
from dv.dsp_models import CicFirChain  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

CIC_STAGES    = int(os.environ.get("CHDEC_CIC_STAGES",   "3"))
CIC_DECIM     = int(os.environ.get("CHDEC_CIC_DECIM",    "10"))
CIC_DELAY     = int(os.environ.get("CHDEC_CIC_DELAY",    "1"))
SAMPLE_W      = int(os.environ.get("CHDEC_SAMPLE_W",     "16"))
FIR_N_TAPS    = int(os.environ.get("CHDEC_FIR_N_TAPS",   "32"))
FIR_COEF_W    = int(os.environ.get("CHDEC_FIR_COEF_W",   "16"))
FIR_OUT_SHIFT = int(os.environ.get("CHDEC_FIR_OUT_SHIFT", str(FIR_COEF_W - 1)))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return (f"channel_decimator__R{CIC_DECIM}_N{CIC_STAGES}_T{FIR_N_TAPS}"
            f"__{testname}")


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"channel_decimator {testname}: "
               f"CIC R={CIC_DECIM} N={CIC_STAGES}, "
               f"FIR taps={FIR_N_TAPS}"),
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="channel_decimator output (model = reference)",
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


async def _program_coefs(axil: AxiLiteMaster, coefs: list[int]) -> None:
    for i, c in enumerate(coefs):
        u = c & ((1 << 32) - 1)
        await axil.write(4 * i, u.to_bytes(4, "little"))


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


def _build_model() -> CicFirChain:
    return CicFirChain(
        cic_stages = CIC_STAGES,
        cic_decim  = CIC_DECIM,
        cic_delay  = CIC_DELAY,
        cic_in_w   = SAMPLE_W,
        cic_out_w  = SAMPLE_W,
        fir_n_taps = FIR_N_TAPS,
        fir_in_w   = SAMPLE_W,
        fir_coef_w = FIR_COEF_W,
        fir_out_w  = SAMPLE_W,
        fir_out_shift = FIR_OUT_SHIFT,
    )


async def _setup_and_program(dut, coefs: list[int]):
    axil, src, sink = await _setup(dut)
    dut.m_axis_tready.value = 1
    await _program_coefs(axil, coefs)
    model = _build_model()
    for i, c in enumerate(coefs):
        model.set_coef(i, c)
    return src, sink, model


# Coefficient sets used by the tests.
def _delta_coefs() -> list[int]:
    """Delta — FIR becomes a pass-through. Output = CIC output."""
    c = [0] * FIR_N_TAPS
    c[0] = (1 << (FIR_COEF_W - 1)) - 1
    return c


def _uniform_coefs() -> list[int]:
    """Uniform moving-average — unity DC gain."""
    val = (1 << (FIR_COEF_W - 1)) // FIR_N_TAPS
    return [val] * FIR_N_TAPS


@pyuvm.test()
class impulse(uvm_test):
    """Impulse fed through delta-coef FIR. Output is the CIC's impulse
    response."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _delta_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            # Enough samples to clear both pipelines.
            n_in = 6 * CIC_DECIM
            samples = [0] * n_in
            samples[0] = (1 << (SAMPLE_W - 2))

            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("impulse", samples, expected, got)
            assert got == expected, (
                f"impulse mismatch ({n_out} samples); first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"impulse: {n_out} output samples from {n_in} inputs, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class dc_step(uvm_test):
    """Sustained DC input + uniform-coef FIR. Output settles to ~DC."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = max(8 * CIC_DECIM, (FIR_N_TAPS + 4) * CIC_DECIM)
            samples = [10000] * n_in
            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("dc_step", samples, expected, got)
            assert got == expected, (
                f"dc_step mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"dc_step: {n_out} samples, bit-exact. "
                f"steady-state out: {got[-1]} (input 10000)")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_passband(uvm_test):
    """Low-frequency tone — passes through the chain."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = 16 * CIC_DECIM
            amp = 1 << (SAMPLE_W - 2)
            # freq_norm low enough to be well within the CIC passband
            samples = [int(amp * math.sin(2 * math.pi * 0.002 * k))
                       for k in range(n_in)]
            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("tone_passband", samples, expected, got)
            assert got == expected, (
                f"tone_passband mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"tone_passband: {n_out} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_stopband(uvm_test):
    """Tone at the CIC's first null (1/DECIM). Strong attenuation. The
    residual is bit-exact between model and RTL."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = 16 * CIC_DECIM
            amp = 1 << (SAMPLE_W - 2)
            f_norm = 1.0 / CIC_DECIM
            samples = [int(amp * math.sin(2 * math.pi * f_norm * k))
                       for k in range(n_in)]
            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("tone_stopband", samples, expected, got)
            assert got == expected, (
                f"tone_stopband mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            out_peak = max(abs(s) for s in got)
            self.logger.info(
                f"tone_stopband: {n_out} samples, bit-exact. "
                f"peak output mag {out_peak} (input amp {amp})")
            assert out_peak < amp / 4, (
                f"tone_stopband: peak output {out_peak} should be well below input {amp}")
        finally:
            self.drop_objection()
