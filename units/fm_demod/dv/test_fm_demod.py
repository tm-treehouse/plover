"""Smoke test for fm_demod.

This commit lands the *wiring* — the integration module instantiates
the four demod-side units, the internal axil_xbar fans out to the
two register-banked sub-units, and the AXIS chain plumbing matches up.

The single test here drives a few hundred IQ samples (synthesized via
dv.fm_synthesis) and verifies that the module produces *some* output
on its audio AXIS master. Bit-exact comparison against the Python
FmDemodChain reference is a follow-up commit.

Why split: this is the first multi-unit integration. Splitting "wiring
compiles and produces output" from "wiring is bit-exact" lets each
commit be reviewable in isolation. A failure to produce *any* output
points at AXIS handshake misrouting; a bit-exact mismatch points at
inter-unit width/order plumbing. The two failure modes are debugged
differently.
"""
from __future__ import annotations

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
from dv.fm_synthesis import (  # noqa: E402
    synthesize_audio_tone, synthesize_audio_dc,
    synthesize_fm_iq_from_audio, synthesize_fm_iq,
)
from dv.dsp_models import FmDemodChain  # noqa: E402

CLK_PERIOD_NS = 10
SAMPLE_W = int(os.environ.get("FMD_SAMPLE_W", "16"))


def _signed(value: int, width: int) -> int:
    v = value & ((1 << width) - 1)
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _pack_iq(i: int, q: int) -> int:
    mask = (1 << SAMPLE_W) - 1
    return ((q & mask) << SAMPLE_W) | (i & mask)


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


# Address map inside fm_demod (4 KB pages).
DEEMPH_BASE = 0x0000   # deemphasis alpha register at offset 0x00
AUDEC_BASE  = 0x1000   # audio_decimator's FIR coefs at offsets 0..N_TAPS*4
N_TAPS      = 16       # matches the default AUDIO_FIR_N_TAPS


def _uniform_coefs() -> list[int]:
    """Uniform FIR coefficients summing to ~1.0 in Q1.15. The same
    pattern the audio_decimator tests use — a poor man's low-pass with
    unity DC gain."""
    val = (1 << 15) // N_TAPS
    return [val] * N_TAPS


async def _program_audio_fir(axil: AxiLiteMaster, coefs: list[int]) -> None:
    """Write an FIR coefficient set via AXI-Lite. Caller is responsible
    for keeping the Python model in sync."""
    for i, c in enumerate(coefs):
        u = c & ((1 << 32) - 1)
        await axil.write(AUDEC_BASE + 4 * i, u.to_bytes(4, "little"))


async def _program_uniform_audio_fir(axil: AxiLiteMaster) -> None:
    """Convenience: program a unity-DC-gain FIR."""
    await _program_audio_fir(axil, _uniform_coefs())


async def _program_deemphasis_alpha(axil: AxiLiteMaster, alpha: int) -> None:
    u = alpha & ((1 << 32) - 1)
    await axil.write(DEEMPH_BASE, u.to_bytes(4, "little"))


def _build_model(alpha: int | None = None,
                 coefs: list[int] | None = None) -> FmDemodChain:
    """Build a model with parameters matching the RTL default config
    and program the same alpha + FIR coefs we wrote via AXI-Lite. The
    caller passes the same alpha and coefs they wrote to the RTL so
    the two sides start with identical state."""
    chain = FmDemodChain()
    if alpha is not None:
        chain.set_deemphasis_alpha(alpha)
    if coefs is not None:
        for i, c in enumerate(coefs):
            chain.set_audio_fir_coef(i, c)
    return chain


async def _drive_iq(src: AxiStreamSource, samples: list[tuple[int, int]]) -> None:
    byte_lanes = (2 * SAMPLE_W + 7) // 8
    for (i, q) in samples:
        raw = _pack_iq(i, q)
        await src.send(raw.to_bytes(byte_lanes, "little"))


async def _collect_audio(sink: AxiStreamSink, n: int) -> list[int]:
    byte_lanes = (SAMPLE_W + 7) // 8
    out: list[int] = []
    for _ in range(n):
        frame = await sink.recv()
        v = int.from_bytes(frame.tdata, "little")
        v &= (1 << (byte_lanes * 8)) - 1
        v &= (1 << SAMPLE_W) - 1
        out.append(_signed(v, SAMPLE_W))
    return out


@pyuvm.test()
class smoke(uvm_test):
    """Drive synthesized FM IQ; verify the chain produces audio output.

    No bit-exact comparison yet — that's the follow-up commit. Here
    we're checking:
      * the module compiles and runs
      * AXIS handshakes through all four sub-units
      * the audio output is *non-trivial* (not all zero, not the
        same value repeated) — confirming the chain is actually
        processing, not stuck.
    """

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            # Program a non-zero FIR — its reset state is all-zero coefs,
            # which silences the chain entirely. Uniform coefs give an
            # easy low-pass with unity DC gain.
            await _program_uniform_audio_fir(axil)

            # A modest audio tone, synthesized into IQ via the Step 3
            # modulator. 400 audio samples upsampled 5x = 2000 IQ samples
            # = 400 audio output samples after the 5x audio decimator.
            audio_in = synthesize_audio_tone(400, amplitude=6000, freq_norm=0.005)
            iq = synthesize_fm_iq_from_audio(
                audio_in, k_f=4000, upsample_ratio=5, amp=1 << 13,
            )
            self.logger.info(f"smoke: driving {len(iq)} IQ samples")

            cocotb.start_soon(_drive_iq(src, iq))
            # Expect ~ len(iq)/5 audio outputs. Collect a bit fewer to
            # avoid stalls if the chain's pipeline fills slightly under
            # the nominal count.
            n_audio = len(iq) // 5 - 4
            got = await _collect_audio(sink, n_audio)
            self.logger.info(
                f"smoke: got {len(got)} audio samples; range "
                f"{min(got)}..{max(got)}")

            # Liveness assertions:
            # 1. We actually got the requested number of samples.
            assert len(got) == n_audio
            # 2. The output isn't all zero (the chain isn't dead).
            assert any(s != 0 for s in got), (
                "smoke: chain produced all-zero output — likely a "
                "wiring or reset issue")
            # 3. The output isn't constant (the chain is actually
            #    processing the time-varying input).
            assert len(set(got)) > 5, (
                f"smoke: chain output has only {len(set(got))} unique "
                "values — too constant for a sinusoidal input, likely a "
                "stuck pipeline")
        finally:
            self.drop_objection()


# ===========================================================================
# Bit-exact tests vs FmDemodChain
# ===========================================================================
#
# Stimulus shape: synthesise a deterministic IQ stream, run it through both
# the RTL and the Python model (with matching coefs + alpha), assert
# byte-identical outputs.
#
# The model's run() produces one output per audio_decimator emit; the RTL
# does the same. Both have identical reset state and identical per-sample
# arithmetic (each sub-unit is bit-exact per its own tests). So if anything
# inside fm_demod.sv has wired things wrong — phase slice off-by-one,
# magnitude/phase swapped, xbar slot misrouted, sub-unit instantiated with
# wrong parameter — the bit-exact comparison catches it immediately.
#
# Settling: the chain has CORDIC's 16-cycle pipeline + 1 each for PhaseDiff
# and Deemphasis + the CIC's startup. The model has none of this — it
# produces an output per input, then the audio_decimator emits every R
# inputs. So the model's run() returns N_in // R outputs (modulo a small
# startup transient inside CIC). The RTL produces the same count.


@pyuvm.test()
class bit_exact_tone(uvm_test):
    """Synthesised FM tone in. Bit-exact comparison vs FmDemodChain."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            coefs = _uniform_coefs()
            await _program_audio_fir(axil, coefs)
            # Use the RTL's reset-default alpha (24820 ~ 0.7574 in Q1.15)
            # — both sides default to this, so no AXI write needed.

            audio_in = synthesize_audio_tone(400, amplitude=6000, freq_norm=0.005)
            iq = synthesize_fm_iq_from_audio(
                audio_in, k_f=4000, upsample_ratio=5, amp=1 << 13,
            )

            # Run the model on the same IQ. The default alpha matches the
            # RTL reset value, so no alpha programming.
            model = _build_model(coefs=coefs)
            expected = model.run(iq)
            n_out = len(expected)
            self.logger.info(
                f"bit_exact_tone: driving {len(iq)} IQ samples, "
                f"expecting {n_out} audio outputs")

            cocotb.start_soon(_drive_iq(src, iq))
            got = await _collect_audio(sink, n_out)

            # Find the first divergence — easier debugging if this fails.
            if got != expected:
                first_diff = next(
                    (i for i, (a, b) in enumerate(zip(got, expected)) if a != b),
                    None,
                )
                ctx = ""
                if first_diff is not None:
                    lo = max(0, first_diff - 2)
                    hi = min(len(got), first_diff + 3)
                    ctx = (
                        f"; first diff at index {first_diff}: "
                        f"got={got[lo:hi]} expected={expected[lo:hi]}"
                    )
                assert False, f"bit_exact_tone: RTL diverges from model{ctx}"

            self.logger.info(
                f"bit_exact_tone: {n_out} samples, bit-exact. "
                f"range got: {min(got)}..{max(got)}")
        finally:
            self.drop_objection()


@pyuvm.test()
class bit_exact_random_iq(uvm_test):
    """Pseudo-random IQ across all four CORDIC quadrants. The hardest
    case for the chain — exercises every CORDIC sign path, every
    PhaseDiff wrap, every FIR sign combination."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            import random
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            coefs = _uniform_coefs()
            await _program_audio_fir(axil, coefs)

            rng = random.Random(0x1337C0DE)
            lim = 1 << 13
            iq = [(rng.randint(-lim, lim - 1), rng.randint(-lim, lim - 1))
                  for _ in range(500)]

            model = _build_model(coefs=coefs)
            expected = model.run(iq)
            n_out = len(expected)

            cocotb.start_soon(_drive_iq(src, iq))
            got = await _collect_audio(sink, n_out)

            if got != expected:
                first_diff = next(
                    (i for i, (a, b) in enumerate(zip(got, expected)) if a != b),
                    None,
                )
                ctx = ""
                if first_diff is not None:
                    lo = max(0, first_diff - 2)
                    hi = min(len(got), first_diff + 3)
                    ctx = (
                        f"; first diff at index {first_diff}: "
                        f"got={got[lo:hi]} expected={expected[lo:hi]}"
                    )
                assert False, f"bit_exact_random_iq: divergence{ctx}"
            self.logger.info(f"bit_exact_random_iq: {n_out} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class bit_exact_dc(uvm_test):
    """Constant IQ (DC carrier, no modulation). Trivial signal but
    catches reset-state and constant-input divergences."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            coefs = _uniform_coefs()
            await _program_audio_fir(axil, coefs)

            # Constant IQ at a moderate amplitude. With zero frequency
            # modulation, PhaseDiff output is the difference of identical
            # phases = 0, deemphasis input is 0, audio_decimator input is
            # 0 — output should be 0 after settling. Bit-exact requires
            # both sides reach zero at the same sample index.
            iq = [(8000, 0)] * 200
            model = _build_model(coefs=coefs)
            expected = model.run(iq)
            n_out = len(expected)

            cocotb.start_soon(_drive_iq(src, iq))
            got = await _collect_audio(sink, n_out)
            assert got == expected, (
                f"bit_exact_dc: divergence; "
                f"first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}; "
                f"got[:5]={got[:5]} expected[:5]={expected[:5]}")
            self.logger.info(f"bit_exact_dc: {n_out} samples, bit-exact")
        finally:
            self.drop_objection()


@pyuvm.test()
class bit_exact_alpha_programming(uvm_test):
    """Like bit_exact_tone but with an explicitly-programmed (non-default)
    deemphasis alpha. Verifies the deemphasis AXI-Lite slot routes
    correctly through the internal xbar and the programmed coefficient
    reaches the Deemphasis unit."""

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            dut = cocotb.top
            axil, src, sink = await _setup(dut)
            dut.m_axis_tready.value = 1

            coefs = _uniform_coefs()
            await _program_audio_fir(axil, coefs)
            # Stronger LPF than default (alpha closer to 1.0 means slower
            # response). 0.95 in Q1.15 = 31130.
            alpha = int(round(0.95 * (1 << 15)))
            await _program_deemphasis_alpha(axil, alpha)

            audio_in = synthesize_audio_tone(400, amplitude=6000, freq_norm=0.005)
            iq = synthesize_fm_iq_from_audio(
                audio_in, k_f=4000, upsample_ratio=5, amp=1 << 13,
            )

            model = _build_model(alpha=alpha, coefs=coefs)
            expected = model.run(iq)
            n_out = len(expected)

            cocotb.start_soon(_drive_iq(src, iq))
            got = await _collect_audio(sink, n_out)
            assert got == expected, (
                f"bit_exact_alpha_programming: divergence; "
                f"first diff index "
                f"{next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), -1)}")
            self.logger.info(
                f"bit_exact_alpha_programming: {n_out} samples, bit-exact "
                f"with alpha={alpha} (~0.95)")
        finally:
            self.drop_objection()

