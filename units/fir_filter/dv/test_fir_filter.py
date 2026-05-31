"""Cocotb tests for fir_filter.

Combines two protocols: AXI-Lite for coefficient programming, AXI-Stream
for sample data. Pattern A throughout — direct cocotb tests, no
sequencer ceremony, but reusing cocotbext-axi's AxiLiteMaster (the same
BFM the project's AxiLiteAgent wraps) for the coefficient bank.

Test scenarios:
* impulse        — delta filter (coef[0]=1, rest=0) reproduces the
                   input one-cycle delayed
* averaging      — uniform coefficients, low-pass behaviour, bit-exact
                   match to the reference
* arbitrary      — Hamming-windowed lowpass; nontrivial coefficient set
* hot_update     — program one filter, run samples, change coefficients
                   mid-stream, confirm output reflects the new coefs
                   on subsequent samples
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
    AxiLiteBus, AxiLiteMaster,
    AxiStreamBus, AxiStreamSource, AxiStreamSink,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import FirFilter  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

N_TAPS    = int(os.environ.get("FIR_N_TAPS",    "8"))
IN_W      = int(os.environ.get("FIR_IN_W",      "16"))
COEF_W    = int(os.environ.get("FIR_COEF_W",    "16"))
OUT_W     = int(os.environ.get("FIR_OUT_W",     "16"))
OUT_SHIFT = int(os.environ.get("FIR_OUT_SHIFT", "15"))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _plot_filename(testname: str) -> str:
    return f"fir_filter__T{N_TAPS}_W{IN_W}-{COEF_W}-{OUT_W}__{testname}"


def _plot(testname: str, inputs, expected, got) -> None:
    plot_test_result(
        filename=_plot_filename(testname),
        title=(f"fir_filter {testname}: "
               f"N_TAPS={N_TAPS}, IN_W={IN_W}, COEF_W={COEF_W}, "
               f"OUT_W={OUT_W}, OUT_SHIFT={OUT_SHIFT}"),
        inputs=inputs,
        expected=expected,
        got=got,
        output_label="output samples",
    )


async def _setup(dut) -> tuple[AxiLiteMaster, AxiStreamSource, AxiStreamSink]:
    """Bring up clock, reset, and the three BFMs (one master per port)."""
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
    """Program coefficients via AXI-Lite writes at word offsets 0..N_TAPS-1.

    Writes the raw COEF_W-bit signed value into a 32-bit AXI-Lite word
    (the RTL takes data[COEF_W-1:0]; the upper bits are ignored on
    write). Reads sign-extend, so what software wrote is what software
    reads back."""
    for i, c in enumerate(coefs):
        u = c & ((1 << 32) - 1)
        await axil.write(4 * i, u.to_bytes(4, "little"))


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


def _build_model() -> FirFilter:
    return FirFilter(n_taps=N_TAPS, in_w=IN_W, coef_w=COEF_W,
                     out_w=OUT_W, out_shift=OUT_SHIFT)


@pyuvm.test()
class impulse(uvm_test):
    """Delta filter (coef[0]=1.0 = 2^(COEF_W-1)-1, rest=0) reproduces
    the input one cycle delayed."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Coefficient set: a "delta at tap 0" → output = input * c0,
            # where c0 = (1 << (COEF_W-1)) - 1 is the largest positive
            # value in Q1.(COEF_W-1) (≈ 1.0).
            c0 = (1 << (COEF_W - 1)) - 1
            coefs = [c0] + [0] * (N_TAPS - 1)

            model = _build_model()
            for i, c in enumerate(coefs):
                model.set_coef(i, c)
            await _program_coefs(axil, coefs)

            # Input: a short stream including the impulse.
            inputs = [0, (1 << (IN_W - 2)), 0, 0,
                      (1 << (IN_W - 3)), -(1 << (IN_W - 3)),
                      0, 0, 0, 0, 0, 0]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("impulse", inputs, expected, got)

            assert got == expected, (
                f"impulse mismatch:\n  expected: {expected}\n"
                f"  got:      {got}\n"
                f"  first diff at index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"impulse: {len(got)} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class averaging(uvm_test):
    """Uniform coefficients = moving average. Step input should
    produce a linear ramp settling to the input level."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Each coef = (1.0 / N_TAPS) in Q1.(COEF_W-1).
            c = ((1 << (COEF_W - 1)) - 1) // N_TAPS
            coefs = [c] * N_TAPS

            model = _build_model()
            for i, cv in enumerate(coefs):
                model.set_coef(i, cv)
            await _program_coefs(axil, coefs)

            # Step input.
            level = 1 << (IN_W - 3)  # ~0.25 in Q1.(IN_W-1)
            inputs = [level] * (N_TAPS * 3)
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("averaging", inputs, expected, got)

            assert got == expected, (
                f"averaging mismatch:\n  expected: {expected[:8]}...\n"
                f"  got:      {got[:8]}...")
            self.logger.info(f"averaging: {len(got)} samples, "
                              f"settled at {got[-1]} (input level {level})")
        finally:
            self.drop_objection()


@pyuvm.test()
class arbitrary(uvm_test):
    """Hamming-windowed coefficients with a random sample stream."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            import math
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Hamming-windowed lowpass-ish; scaled into Q1.(COEF_W-1).
            scale = (1 << (COEF_W - 1)) - 1
            window = [0.54 - 0.46 * math.cos(2 * math.pi * i / (N_TAPS - 1))
                      for i in range(N_TAPS)]
            wsum = sum(window) or 1.0
            coefs = [int(scale * w / wsum / N_TAPS) for w in window]
            # Ensure they fit COEF_W signed; clamp defensively.
            lim = (1 << (COEF_W - 1)) - 1
            coefs = [max(-lim, min(lim, c)) for c in coefs]

            model = _build_model()
            for i, cv in enumerate(coefs):
                model.set_coef(i, cv)
            await _program_coefs(axil, coefs)

            rng = random.Random(0xBADC_0DE)
            n_inputs = 64
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1
            inputs = [rng.randint(lo, hi) for _ in range(n_inputs)]
            expected = model.run(inputs)

            send_task = cocotb.start_soon(_send_samples(src, inputs))
            got = await _collect_samples(sink, len(expected))
            await send_task

            _plot("arbitrary", inputs, expected, got)

            assert got == expected, (
                f"arbitrary mismatch ({len(got)} samples); first diff: "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"arbitrary: {len(got)} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class hot_update(uvm_test):
    """Program coefficient set A, push some samples, change to set B
    mid-stream via AXI-Lite, push more samples. Output for the post-
    update samples must reflect set B (with the appropriate sample
    history). The Python reference does the same swap at the same
    sample index."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Set A: delta filter (coef[0]=max-positive, others 0).
            cA = (1 << (COEF_W - 1)) - 1
            coefs_A = [cA] + [0] * (N_TAPS - 1)
            # Set B: averager.
            cB = ((1 << (COEF_W - 1)) - 1) // N_TAPS
            coefs_B = [cB] * N_TAPS

            model = _build_model()
            for i, c in enumerate(coefs_A): model.set_coef(i, c)
            await _program_coefs(axil, coefs_A)

            # First half: 16 samples with coefs_A.
            rng = random.Random(7)
            lo = -(1 << (IN_W - 1))
            hi =  (1 << (IN_W - 1)) - 1
            n_a = 16
            inputs_a = [rng.randint(lo, hi) for _ in range(n_a)]
            expected_a = [model.step(s) for s in inputs_a]

            # Push first half, collect.
            send_task_a = cocotb.start_soon(_send_samples(src, inputs_a))
            got_a = await _collect_samples(sink, n_a)
            await send_task_a

            # Swap to set B (both in the model and via AXI-Lite).
            for i, c in enumerate(coefs_B): model.set_coef(i, c)
            await _program_coefs(axil, coefs_B)

            # Second half: 16 more samples with coefs_B.
            n_b = 16
            inputs_b = [rng.randint(lo, hi) for _ in range(n_b)]
            expected_b = [model.step(s) for s in inputs_b]

            send_task_b = cocotb.start_soon(_send_samples(src, inputs_b))
            got_b = await _collect_samples(sink, n_b)
            await send_task_b

            inputs   = inputs_a   + inputs_b
            expected = expected_a + expected_b
            got      = got_a      + got_b

            _plot("hot_update", inputs, expected, got)

            assert got == expected, (
                f"hot_update mismatch ({len(got)} samples); first diff: "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}\n"
                f"  expected around diff: ...{expected[max(0,next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), len(got))-2):]}\n"
                f"  got around diff:      ...{got[max(0,next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), len(got))-2):]}")
            self.logger.info(
                f"hot_update: {n_a} with coefs_A + {n_b} with coefs_B, bit-exact")
        finally:
            self.drop_objection()
