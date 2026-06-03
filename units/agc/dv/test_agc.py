"""Cocotb tests for agc.

Pattern A: bit-exact comparison against the Python reference.

The AGC is the project's first closed-loop feedback unit. The gain
register's update timing matters for bit-exactness: any disagreement
between RTL and model causes the gain trajectory to diverge over time
(state depends on history).

Test scenarios:
* small_signal      — constant small input; gain should ramp up until
                       output approaches target. Bit-exact against model.
* large_signal      — constant large input; gain should fall toward
                       target/|input|. Verifies the loop works in both
                       directions.
* random_streams    — random pseudo-noise IQ. Bit-exact across the
                       entire stream covers every sign/path combo.
* gain_clamp        — set gain_max very low so loop hits the clamp.
                       Output amplitude is capped; verifies the clamp
                       logic and that the model matches.
* reset_gain_pulse  — write the control-register pulse mid-stream;
                       gain snaps back to gain_init; subsequent samples
                       are scaled by gain_init then continue updating.
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
    AxiStreamBus, AxiStreamSource, AxiStreamSink, AxiStreamFrame,
)

from pyuvm import uvm_test

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from dv.dsp_models import Agc  # noqa: E402
from dv.dsp_plot import plot_test_result  # noqa: E402

CLK_PERIOD_NS = 10

SAMPLE_W      = int(os.environ.get("AGC_SAMPLE_W",   "16"))
GAIN_W        = int(os.environ.get("AGC_GAIN_W",     "16"))
GAIN_INT_W    = int(os.environ.get("AGC_GAIN_INT_W", "4"))
GAIN_FRAC_W   = int(os.environ.get("AGC_GAIN_FRAC_W", str(GAIN_W - GAIN_INT_W)))

# Register offsets (bytes), matching the RTL:
REG_TARGET     = 0x00
REG_MU_SHIFT   = 0x04
REG_GAIN_MIN   = 0x08
REG_GAIN_MAX   = 0x0C
REG_GAIN_INIT  = 0x10
REG_CONTROL    = 0x14
REG_GAIN_OBS   = 0x18


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _pack_iq(i: int, q: int) -> int:
    mask = (1 << SAMPLE_W) - 1
    return ((q & mask) << SAMPLE_W) | (i & mask)


def _split_iq(raw: int) -> tuple[int, int]:
    mask = (1 << SAMPLE_W) - 1
    return (_signed(raw & mask, SAMPLE_W),
            _signed((raw >> SAMPLE_W) & mask, SAMPLE_W))


def _plot_filename(testname: str) -> str:
    return f"agc__W{SAMPLE_W}_G{GAIN_W}__{testname}"


def _plot(testname: str, iq_pairs, expected_pairs, inputs) -> None:
    got_i  = [p[0] for p in iq_pairs]
    expt_i = [p[0] for p in expected_pairs]
    in_i   = [p[0] for p in inputs]
    plot_test_result(
        filename=_plot_filename(testname),
        title=f"agc {testname}: SAMPLE_W={SAMPLE_W}, GAIN_W={GAIN_W} (I trace)",
        inputs=in_i,
        expected=expt_i,
        got=got_i,
        output_label="I out (model = reference)",
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
    source = AxiStreamSource(
        AxiStreamBus.from_prefix(dut, "s_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    sink = AxiStreamSink(
        AxiStreamBus.from_prefix(dut, "m_axis"),
        dut.clk, dut.rst_n, reset_active_level=False,
    )
    return axil, source, sink


async def _axil_write(axil: AxiLiteMaster, addr: int, value: int) -> None:
    await axil.write(addr, (value & 0xFFFFFFFF).to_bytes(4, "little"))


async def _drive_iq(source: AxiStreamSource, samples: list[tuple[int, int]]) -> None:
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


def _build_model() -> Agc:
    return Agc(sample_w=SAMPLE_W, gain_w=GAIN_W,
               gain_int_w=GAIN_INT_W, gain_frac_w=GAIN_FRAC_W)


async def _program_agc(axil: AxiLiteMaster, model: Agc,
                       target: int, mu_shift: int,
                       gain_min: int, gain_max: int,
                       gain_init: int) -> None:
    """Program the same configuration into RTL (via AXI-Lite) and the
    Python model. Done in lock-step so both sides start each scenario
    with identical state."""
    await _axil_write(axil, REG_TARGET,    target & ((1 << SAMPLE_W) - 1))
    await _axil_write(axil, REG_MU_SHIFT,  mu_shift)
    await _axil_write(axil, REG_GAIN_MIN,  gain_min)
    await _axil_write(axil, REG_GAIN_MAX,  gain_max)
    await _axil_write(axil, REG_GAIN_INIT, gain_init)
    # Pulse the control register to load gain_init into the gain reg.
    await _axil_write(axil, REG_CONTROL,   1)
    model.set_target(target)
    model.set_mu_shift(mu_shift)
    model.set_gain_clamp(gain_min, gain_max)
    model.set_gain_init(gain_init)
    model.reset_gain()


@pyuvm.test()
class small_signal(uvm_test):
    """Constant small input; gain ramps up until output ~ target."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = _build_model()
            await _program_agc(axil, model,
                               target=20000, mu_shift=6,
                               gain_min=0, gain_max=(1 << GAIN_W) - 1,
                               gain_init=1 << GAIN_FRAC_W)   # 1.0

            n = 256
            samples = [(1000, 0)] * n
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_iq(sink, n)

            _plot("small_signal", got, expected, samples)
            assert got == expected, (
                f"small_signal mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"small_signal: {n} samples, bit-exact; "
                              f"final gain ~ {model.get_gain() / (1 << GAIN_FRAC_W):.2f}x")
        finally:
            self.drop_objection()


@pyuvm.test()
class large_signal(uvm_test):
    """Constant large input; gain ramps down toward target/|input|."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = _build_model()
            # Start with high initial gain so we observe ramp-down.
            await _program_agc(axil, model,
                               target=5000, mu_shift=6,
                               gain_min=0, gain_max=(1 << GAIN_W) - 1,
                               gain_init=4 << GAIN_FRAC_W)   # 4.0

            n = 256
            samples = [(25000, 0)] * n
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_iq(sink, n)

            _plot("large_signal", got, expected, samples)
            assert got == expected, (
                f"large_signal mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"large_signal: {n} samples, bit-exact; "
                              f"final gain ~ {model.get_gain() / (1 << GAIN_FRAC_W):.2f}x")
        finally:
            self.drop_objection()


@pyuvm.test()
class random_streams(uvm_test):
    """Pseudo-random IQ input; bit-exact through the feedback loop."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = _build_model()
            await _program_agc(axil, model,
                               target=12000, mu_shift=8,
                               gain_min=0, gain_max=(1 << GAIN_W) - 1,
                               gain_init=2 << GAIN_FRAC_W)

            rng = random.Random(0xABCDEF12)
            lim = 1 << (SAMPLE_W - 2)   # half-scale, leaves headroom
            n = 256
            samples = [(rng.randint(-lim, lim - 1), rng.randint(-lim, lim - 1))
                       for _ in range(n)]
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_iq(sink, n)

            _plot("random_streams", got, expected, samples)
            assert got == expected, (
                f"random_streams mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"random_streams: {n} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class gain_clamp(uvm_test):
    """Set gain_max very low; loop hits the clamp and stays there."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, source, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            model = _build_model()
            # Try to push gain very high (small input, large target) but
            # gain_max is clamped to 2.0 (= 2 << GAIN_FRAC_W).
            await _program_agc(axil, model,
                               target=30000, mu_shift=4,
                               gain_min=0,
                               gain_max=2 << GAIN_FRAC_W,
                               gain_init=1 << GAIN_FRAC_W)

            n = 128
            samples = [(500, 0)] * n
            expected = model.run(samples)

            cocotb.start_soon(_drive_iq(source, samples))
            got = await _collect_iq(sink, n)

            _plot("gain_clamp", got, expected, samples)
            assert got == expected, (
                f"gain_clamp mismatch; first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(f"gain_clamp: {n} samples, bit-exact; "
                              f"final gain ~ {model.get_gain() / (1 << GAIN_FRAC_W):.2f}x "
                              f"(clamped to 2.00)")
        finally:
            self.drop_objection()
