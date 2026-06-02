"""Cocotb tests for dc_blocker.

Pattern A: bit-exact comparison against the Python reference. The DC
blocker is the project's first IIR — the feedback path means an
arithmetic disagreement compounds quickly, so the bit-exactness is
particularly load-bearing here.

Test scenarios:
* dc_step      — programs alpha, drives a DC step input. Output should
                  settle exponentially toward zero (the DC is killed).
* impulse      — drives a single nonzero sample. Output is the IIR
                  impulse response: a positive spike, then an
                  exponential decay (with the sign flipped by the
                  z^-1 in the numerator).
* sinusoid     — passes a mid-band tone through. Should come out
                  mostly intact (slight gain near unity at high freq).
* alpha_update — starts with alpha=0 (pure differentiator y = x-x_prev),
                  then programs alpha mid-stream. The Python model
                  receives the same update at the same sample index;
                  bit-exact agreement required across the boundary.
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
    AxiLiteBus, AxiLiteMaster,
    AxiStreamBus, AxiStreamSource, AxiStreamSink,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import DcBlocker  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

IN_W        = int(os.environ.get("DCB_IN_W",        "16"))
COEF_W      = int(os.environ.get("DCB_COEF_W",      "16"))
OUT_W       = int(os.environ.get("DCB_OUT_W",       "16"))
COEF_INT_W  = int(os.environ.get("DCB_COEF_INT_W",  "1"))
COEF_FRAC_W = int(os.environ.get("DCB_COEF_FRAC_W", str(COEF_W - 1)))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return f"dc_blocker__W{IN_W}-{COEF_W}-{OUT_W}__{testname}"


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"dc_blocker {testname}: "
               f"IN_W={IN_W}, COEF_W={COEF_W}, OUT_W={OUT_W}, "
               f"COEF_FRAC_W={COEF_FRAC_W}"),
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="DC-blocked output",
    )


async def _setup(dut) -> tuple[AxiLiteMaster, AxiStreamSource, AxiStreamSink]:
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


def _build_model() -> DcBlocker:
    return DcBlocker(in_w=IN_W, coef_w=COEF_W, out_w=OUT_W,
                     coef_int_w=COEF_INT_W, coef_frac_w=COEF_FRAC_W)


def _alpha_for(value_float: float) -> int:
    """Convert a real value < 1.0 to the COEF_W-wide Q signed
    representation the RTL expects."""
    scale = (1 << COEF_FRAC_W)
    return int(value_float * scale)


@pyuvm.test()
class dc_step(uvm_test):
    """Program alpha=0.995, push a DC step, confirm exponential
    decay to zero (the DC suppression)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            alpha = _alpha_for(0.995)
            model = _build_model()
            model.set_alpha(alpha)
            await _program_alpha(axil, alpha)

            n = 200
            inputs = [10000] * n
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, n)
            await send_task

            _plot("dc_step", inputs, expected, got)

            assert got == expected, (
                f"dc_step mismatch ({n} samples); first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            # Sanity check on the model itself — at this alpha (~0.995),
            # the time constant is ~200 samples, so y[~200] should be
            # ~e^-1 ≈ 36.8% of the initial transient amplitude.
            decay_ratio = abs(got[199]) / abs(got[0]) if got[0] else 0
            self.logger.info(
                f"dc_step: {len(got)} samples, bit-exact. "
                f"Decay ratio y[199]/y[0] = {decay_ratio:.3f} "
                f"(theory: ~0.37 for alpha=0.995)")
        finally:
            self.drop_objection()


@pyuvm.test()
class impulse(uvm_test):
    """Impulse response: one nonzero input sample, observe the
    characteristic IIR decay."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            alpha = _alpha_for(0.99)
            model = _build_model()
            model.set_alpha(alpha)
            await _program_alpha(axil, alpha)

            n = 64
            inputs = [0] * n
            inputs[0] = (1 << (IN_W - 2))  # 0.5 in Q1.(IN_W-1)
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, n)
            await send_task

            _plot("impulse", inputs, expected, got)

            assert got == expected, (
                f"impulse mismatch:\n  expected (first 8): {expected[:8]}\n"
                f"  got      (first 8): {got[:8]}")
            self.logger.info(f"impulse: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class sinusoid(uvm_test):
    """Pass a mid-band tone. The DC blocker should leave it almost
    intact (high-frequency gain approaches 2/(1+alpha) ≈ 1.0)."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            alpha = _alpha_for(0.99)
            model = _build_model()
            model.set_alpha(alpha)
            await _program_alpha(axil, alpha)

            n = 128
            amp = 1 << (IN_W - 3)
            inputs = [int(amp * math.sin(2 * math.pi * 0.1 * k))
                      for k in range(n)]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, n)
            await send_task

            _plot("sinusoid", inputs, expected, got)

            assert got == expected, (
                f"sinusoid mismatch ({n} samples); first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"sinusoid: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class alpha_update(uvm_test):
    """Program alpha=0 (pure differentiator), push some samples,
    update alpha=0.99 mid-stream, push more. The Python model
    receives the same update at the same sample index; bit-exact
    agreement is required across the boundary."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = _build_model()
            # Default alpha is 0; explicit for clarity.
            model.set_alpha(0)
            await _program_alpha(axil, 0)

            rng = random.Random(0xDEAD_CAFE)
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1

            n_a = 32
            inputs_a = [rng.randint(lo, hi) for _ in range(n_a)]
            expected_a = [model.step(s) for s in inputs_a]
            send_a = cocotb.start_soon(_send_samples(src, inputs_a))
            got_a = await _collect_samples(sink, n_a)
            await send_a

            # Mid-stream update
            new_alpha = _alpha_for(0.99)
            model.set_alpha(new_alpha)
            await _program_alpha(axil, new_alpha)

            n_b = 32
            inputs_b = [rng.randint(lo, hi) for _ in range(n_b)]
            expected_b = [model.step(s) for s in inputs_b]
            send_b = cocotb.start_soon(_send_samples(src, inputs_b))
            got_b = await _collect_samples(sink, n_b)
            await send_b

            inputs   = inputs_a   + inputs_b
            expected = expected_a + expected_b
            got      = got_a      + got_b

            _plot("alpha_update", inputs, expected, got)

            assert got == expected, (
                f"alpha_update mismatch ({len(got)} samples); first diff "
                f"at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"alpha_update: {n_a} with alpha=0 + {n_b} with alpha=0.99, "
                "bit-exact across the boundary")
        finally:
            self.drop_objection()
