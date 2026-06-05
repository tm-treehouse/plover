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
    synthesize_audio_tone, synthesize_fm_iq_from_audio,
)

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
AUDEC_BASE = 0x1000   # audio_decimator's FIR coefs at offsets 0..N_TAPS*4
N_TAPS     = 16       # matches the default AUDIO_FIR_N_TAPS


async def _program_uniform_audio_fir(axil: AxiLiteMaster) -> None:
    """Program a uniform-coefficient FIR (low-pass with unity DC gain).
    Without this, the FIR's reset-zero coefficients silence the chain."""
    val = (1 << 15) // N_TAPS
    for i in range(N_TAPS):
        u = val & ((1 << 32) - 1)
        await axil.write(AUDEC_BASE + 4 * i, u.to_bytes(4, "little"))


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
