"""Model-only tests for the FmDemodChain composition reference.

No RTL, no FuseSoC, no cocotb — pure Python. These run fast (sub-second
each) and validate that the FmDemodChain class correctly composes the
underlying sub-models. They're the host-side analogue of the unit
testbenches: each unit's bit-exactness against its RTL is verified by
its own cocotb test; this file checks that the *composition* doesn't
introduce surprises.

What's verified:
* Construction and parameter consistency.
* Output count matches the audio decimator's expected rate.
* Composition equivalence: running the chain class produces exactly
  the same output as manually stitching the four sub-models, given
  identical parameters and inputs. This is the strongest kind of
  test for a wrapper — it proves the wrapper has no hidden behaviour.
* PHASE_W != SAMPLE_W assertion fires at construction.

Not verified (deferred to step 4):
* Bit-exact agreement against the eventual demod sub-chain RTL.
  That requires the RTL to exist; for now the model is the spec.
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from dv.dsp_models import (  # noqa: E402
    Cordic, PhaseDiff, Deemphasis, CicFirChain, FmDemodChain,
)


def _synth_fm_iq(n: int, amp: int = 1 << 13,
                 carrier_omega: float = 0.05,
                 mod_depth: float = 0.02,
                 mod_omega: float = 2 * math.pi / 50) -> list[tuple[int, int]]:
    """Generate a small FM-modulated IQ stream. Carrier frequency is
    ``carrier_omega`` rad/sample modulated by ``mod_depth`` *
    cos(mod_omega * k). Not intended to be a realistic FM broadcast
    signal — just a deterministic input that exercises every sub-model."""
    samples: list[tuple[int, int]] = []
    phase = 0.0
    for k in range(n):
        omega = carrier_omega + mod_depth * math.cos(mod_omega * k)
        phase += omega
        i = int(amp * math.cos(phase))
        q = int(amp * math.sin(phase))
        samples.append((i, q))
    return samples


def _program_unity_fir(chain: FmDemodChain) -> None:
    """Program the chain's audio FIR with uniform coefficients (sum ~ 1.0).
    Gives unity DC gain for an arbitrary input."""
    n_taps = chain._audio.fir.N_TAPS
    val = (1 << 15) // n_taps
    for i in range(n_taps):
        chain.set_audio_fir_coef(i, val)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_defaults_compose_cleanly(self):
        c = FmDemodChain()
        assert c.SAMPLE_W == 16
        assert c.PHASE_W == 16

    def test_phase_width_mismatch_rejected(self):
        # PHASE_W != SAMPLE_W must fail loudly: the PhaseDiff output
        # feeds Deemphasis as same-width samples; mismatched widths
        # would silently lose precision or wrap.
        with pytest.raises(AssertionError, match="must equal SAMPLE_W"):
            FmDemodChain(sample_w=16, phase_w=20)

    def test_custom_audio_params_accepted(self):
        c = FmDemodChain(
            sample_w=16, phase_w=16,
            cic_stages=2, cic_decim=4, cic_delay=1,
            fir_n_taps=12, fir_coef_w=16,
        )
        assert c._audio.fir.N_TAPS == 12


# ---------------------------------------------------------------------------
# Output count and rate behaviour
# ---------------------------------------------------------------------------

class TestRate:
    def test_output_count_matches_cic_ratio(self):
        """With CIC_DECIM=5, 500 input IQ samples should produce
        exactly 500/5 = 100 audio outputs."""
        c = FmDemodChain()
        _program_unity_fir(c)
        n = 500
        samples = _synth_fm_iq(n)
        audio = c.run(samples)
        assert len(audio) == n // 5

    def test_step_returns_none_between_outputs(self):
        """4 of every 5 step() calls should return None (no audio
        sample produced this cycle)."""
        c = FmDemodChain()
        _program_unity_fir(c)
        samples = _synth_fm_iq(20)
        results = [c.step(s) for s in samples]
        produced = sum(1 for r in results if r is not None)
        # CIC starts emitting after its DELAY * STAGES * R = 1 * 3 * 5
        # = 15-cycle initialization. After that, one every R=5. For
        # n=20, we expect about 1 emitted sample (depends on alignment).
        assert produced == len(samples) // 5


# ---------------------------------------------------------------------------
# Composition equivalence — the most important test
# ---------------------------------------------------------------------------

class TestCompositionEquivalence:
    """The wrapper must produce exactly the same output as manually
    stitching the four sub-models with the same parameters. Any
    divergence indicates the wrapper has introduced behaviour that
    isn't in the underlying models."""

    def _run_manual_chain(self, samples, fir_coefs) -> list[int]:
        """Manually compose Cordic + PhaseDiff + Deemphasis + CicFirChain
        without going through FmDemodChain. Must produce the same output
        as the wrapper for the same inputs and parameters."""
        cordic = Cordic(sample_w=16, phase_w=16, iterations=16)
        phasediff = PhaseDiff(phase_w=16)
        deemphasis = Deemphasis(sample_w=16, coef_w=16)
        audio = CicFirChain(
            cic_stages=3, cic_decim=5, cic_delay=1,
            cic_in_w=16, cic_out_w=16,
            fir_n_taps=16, fir_in_w=16,
            fir_coef_w=16, fir_out_w=16,
        )
        for i, c in enumerate(fir_coefs):
            audio.set_coef(i, c)

        out: list[int] = []
        for iq in samples:
            _mag, phase = cordic.step(iq)
            freq = phasediff.step(phase)
            d = deemphasis.step(freq)
            r = audio.step(d)
            if r is not None:
                out.append(r)
        return out

    def test_equivalence_synth_fm(self):
        """Synthesized FM signal — exercises every sub-model with a
        deterministic non-trivial input."""
        samples = _synth_fm_iq(500)

        # Same coefs both sides.
        n_taps = 16
        val = (1 << 15) // n_taps
        coefs = [val] * n_taps

        chain = FmDemodChain()
        for i, c in enumerate(coefs):
            chain.set_audio_fir_coef(i, c)
        wrapper_out = chain.run(samples)

        manual_out = self._run_manual_chain(samples, coefs)

        assert wrapper_out == manual_out, (
            f"FmDemodChain output diverges from manual composition; "
            f"first diff index "
            f"{next((i for i, (a, b) in enumerate(zip(wrapper_out, manual_out)) if a != b), -1)}")

    def test_equivalence_random_iq(self):
        """Random IQ samples across the full signed range — covers
        all quadrants in the CORDIC and exercises sign-handling
        through PhaseDiff and the audio chain."""
        rng = random.Random(0xCAFEBABE)
        lim = 1 << 13
        samples = [(rng.randint(-lim, lim - 1), rng.randint(-lim, lim - 1))
                   for _ in range(300)]

        n_taps = 16
        coefs = [(rng.randint(-1000, 5000)) for _ in range(n_taps)]

        chain = FmDemodChain()
        for i, c in enumerate(coefs):
            chain.set_audio_fir_coef(i, c)
        wrapper_out = chain.run(samples)
        manual_out = self._run_manual_chain(samples, coefs)
        assert wrapper_out == manual_out

    def test_equivalence_zero_input(self):
        """All-zero input: every sub-model should sit at its reset
        state, and the chain output should match the manual chain
        (both producing all-zero or near-zero audio)."""
        samples = [(0, 0)] * 100
        coefs = [0x0800] * 16   # arbitrary small coefs

        chain = FmDemodChain()
        for i, c in enumerate(coefs):
            chain.set_audio_fir_coef(i, c)
        wrapper_out = chain.run(samples)
        manual_out = self._run_manual_chain(samples, coefs)
        assert wrapper_out == manual_out
