"""Cocotb tests for audio_decimator.

Pattern A: bit-exact comparison against the Python reference. The
reference is the existing CicFirChain model in dv/dsp_models.py
configured with the same CIC + FIR parameters as the wrapper — same
class the standalone CIC-FIR chain test uses, just exercised at
audio-rate parameters.

The wrapper itself has no arithmetic, so this test is really a
"composition" test: the bit-exactness of the underlying units flows
through to the wrapper's output as long as the handshake between
CIC and FIR is identical to the standalone CIC-FIR chain (which the
existing top-level tests have already validated).

Test scenarios:
* impulse        — single-sample impulse fed through. Output is the
                    FIR's impulse response sampled at the CIC's
                    output rate.
* dc_step        — sustained DC input. Both CIC and FIR have unity
                    DC gain (for an all-ones FIR; coefs are
                    normalised below).
* tone_passband  — sinusoid at a frequency in the audio passband.
                    Should pass through with modest attenuation.
* tone_stopband  — sinusoid above the FIR cutoff. Should be more
                    strongly attenuated. Bit-exact in both cases.

Default audio-rate parameters:
* CIC: STAGES=3, DECIM=5, DELAY=1  (~2.4 MS/s -> 480 kS/s is too
  much for one stage; this test uses fs=250 kS/s -> 50 kS/s which
  is the second-stage decimation, post-channel-filter)
* FIR: 16 taps, Q1.15 coefficients
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

CIC_STAGES   = int(os.environ.get("AUDEC_CIC_STAGES",  "3"))
CIC_DECIM    = int(os.environ.get("AUDEC_CIC_DECIM",   "5"))
CIC_DELAY    = int(os.environ.get("AUDEC_CIC_DELAY",   "1"))
SAMPLE_W     = int(os.environ.get("AUDEC_SAMPLE_W",    "16"))
FIR_N_TAPS   = int(os.environ.get("AUDEC_FIR_N_TAPS",  "16"))
FIR_COEF_W   = int(os.environ.get("AUDEC_FIR_COEF_W",  "16"))
FIR_OUT_SHIFT = int(os.environ.get("AUDEC_FIR_OUT_SHIFT", str(FIR_COEF_W - 1)))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return (f"audio_decimator__R{CIC_DECIM}_N{CIC_STAGES}_T{FIR_N_TAPS}"
            f"__{testname}")


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"audio_decimator {testname}: "
               f"CIC R={CIC_DECIM} N={CIC_STAGES}, "
               f"FIR taps={FIR_N_TAPS}"),
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="audio_decimator output (model = reference)",
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
    """Write FIR coefficients at word offsets 0..N_TAPS-1."""
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
    """Build a CicFirChain configured to match the RTL parameters."""
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
    """Wires up the BFMs, programs FIR coefficients into RTL and model
    in lock-step, returns (src, sink, model)."""
    axil, src, sink = await _setup(dut)
    dut.m_axis_tready.value = 1
    await _program_coefs(axil, coefs)
    model = _build_model()
    for i, c in enumerate(coefs):
        model.set_coef(i, c)
    return src, sink, model


# Coefficient sets used by the tests.
#
# DELTA_COEFS: a delta — first tap = 1.0, rest = 0. The FIR becomes a
# pass-through, so the wrapper's output is just the CIC's output.
def _delta_coefs() -> list[int]:
    c = [0] * FIR_N_TAPS
    c[0] = (1 << (FIR_COEF_W - 1)) - 1   # nearly 1.0 in Q1.15
    return c


# UNIFORM_COEFS: an N-tap moving-average (a poor man's lowpass). Sum
# of coefs = ~1.0, so DC gain is unity.
def _uniform_coefs() -> list[int]:
    val = (1 << (FIR_COEF_W - 1)) // FIR_N_TAPS
    return [val] * FIR_N_TAPS


@pyuvm.test()
class impulse(uvm_test):
    """Impulse fed through delta-coef FIR. Output is the CIC's impulse
    response only."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _delta_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            # Enough input samples to clear the CIC and FIR pipelines.
            n_in = 8 * CIC_DECIM
            samples = [0] * n_in
            samples[0] = (1 << (SAMPLE_W - 2))   # nice mid-scale impulse

            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("impulse", samples, expected, got)
            assert got == expected, (
                f"impulse mismatch ({n_out} samples); first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"impulse: {n_out} output samples (from {n_in} inputs), bit-exact. "
                f"output: {got[:6]}")
        finally:
            self.drop_objection()


@pyuvm.test()
class dc_step(uvm_test):
    """Sustained DC input. Output should settle to an attenuated DC
    value (uniform FIR has unity DC gain modulo truncation drift)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = 16 * CIC_DECIM
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
                f"dc_step: {n_out} output samples, bit-exact. "
                f"steady-state out: {got[-1]} (input 10000)")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_passband(uvm_test):
    """Sinusoid at a frequency low enough to pass through the chain."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = 32 * CIC_DECIM
            amp = 1 << (SAMPLE_W - 2)
            # freq_norm = 0.005 cycles/sample at input rate — very low,
            # passes both CIC (no droop near DC) and FIR (well below
            # any cutoff).
            samples = [int(amp * math.sin(2 * math.pi * 0.005 * k))
                       for k in range(n_in)]
            expected = model.run(samples)
            n_out = len(expected)

            cocotb.start_soon(_send_samples(src, samples))
            got = await _collect_samples(sink, n_out)

            _plot("tone_passband", samples, expected, got)
            assert got == expected, (
                f"tone_passband mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"tone_passband: {n_out} output samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class tone_stopband(uvm_test):
    """Sinusoid at a frequency near the CIC's first null. Strong
    attenuation expected. Bit-exact comparison still holds — the
    attenuation is the *shared* behaviour of both the model and RTL."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            coefs = _uniform_coefs()
            src, sink, model = await _setup_and_program(dut, coefs)

            n_in = 32 * CIC_DECIM
            amp = 1 << (SAMPLE_W - 2)
            # freq_norm = 1.0/CIC_DECIM at the input rate — this lands
            # exactly on the CIC's first null, so the output should
            # be ~zero. Bit-exact comparison verifies that "zero" is
            # the same zero both sides compute.
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
            # Sanity: outputs at the CIC null should be small.
            out_peak = max(abs(s) for s in got)
            self.logger.info(
                f"tone_stopband: {n_out} output samples, bit-exact. "
                f"peak output mag {out_peak} (input amp {amp}); "
                f"strong attenuation expected near CIC null")
            assert out_peak < amp / 4, (
                f"tone_stopband: peak output {out_peak} should be well below input {amp}")
        finally:
            self.drop_objection()
