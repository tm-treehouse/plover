"""Closed-loop roundtrip tests: modulate -> demodulate -> compare.

Synthesises FM IQ from a known audio signal using dv/fm_synthesis.py,
demodulates with FmDemodChain, and checks that the recovered audio is
recognisably the input.

Unlike the unit tests (bit-exact) and the FmDemodChain composition tests
(also bit-exact), these are *tolerance-based*. The signal flow through
the loop is:

    audio -> pre-emphasis -> sample-and-hold upsample -> phase integrate
          -> IQ baseband -> CORDIC vectoring -> phase differentiator
          -> de-emphasis IIR -> CIC + FIR audio decimation -> recovered audio

Each stage introduces some error: pre-emphasis IIR truncation drift,
sample-and-hold sinc rolloff (the dominant high-freq attenuator),
CORDIC quantisation (~2^-16 phase), de-emphasis IIR drift, CIC droop,
FIR truncation. The recovered audio is a scaled, slightly delayed,
slightly band-limited version of the input.

Acceptance criteria are calibrated empirically against a working chain:
SNR > 35 dB for low audio frequencies, gracefully degrading toward 20 dB
at higher frequencies near the audio_decimator's cutoff.

Sample rate convention here: the chain's "audio rate" is the baseband
rate / CIC_DECIM = audio_decim's output rate. With CIC_DECIM=5 and a
notional 2.4 MS/s baseband, audio rate is 480 kS/s. We don't try to
reach a real broadcast 48 kS/s audio rate — that would need an
additional ~10x decimation that the audio_decimator doesn't currently
do. The test rates are picked to make the chain consistent end-to-end,
not to match a specific broadcast standard.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from dv.dsp_models import FmDemodChain  # noqa: E402
from dv.fm_synthesis import (  # noqa: E402
    synthesize_audio_dc,
    synthesize_audio_step,
    synthesize_audio_tone,
    synthesize_audio_multitone,
    synthesize_fm_iq_from_audio,
    snr_db,
    best_match_scale_offset,
)


# Match the audio_decimator's defaults: CIC_DECIM=5, FIR taps=16.
UPSAMPLE_RATIO = 5
K_F            = 4000          # moderate FM deviation in phase-lsb / sample
AMP            = 1 << 13       # IQ amplitude
N_TAPS         = 16            # matches FmDemodChain default
SKIP_SETTLE    = 60            # samples to skip for transient settling


def _build_chain() -> FmDemodChain:
    """Build a chain with a unity-DC FIR (uniform coefficients)."""
    chain = FmDemodChain()
    val = (1 << 15) // N_TAPS
    for i in range(N_TAPS):
        chain.set_audio_fir_coef(i, val)
    return chain


def _roundtrip(audio_in: list[int]) -> list[int]:
    """Modulate then demodulate. Returns recovered audio."""
    iq = synthesize_fm_iq_from_audio(
        audio_in, k_f=K_F, upsample_ratio=UPSAMPLE_RATIO, amp=AMP,
    )
    chain = _build_chain()
    return chain.run(iq)


def _fit_snr(reference: list[int], measurement: list[int]) -> tuple[float, float, float]:
    """Best-fit (scale, offset) and return (scale, offset, SNR_dB) on
    the post-fit residual. The chain has a constant gain and offset
    relative to the input; SNR after removing those is the meaningful
    fidelity metric."""
    n = min(len(reference), len(measurement))
    ref = reference[SKIP_SETTLE:SKIP_SETTLE + n - SKIP_SETTLE]
    meas = measurement[SKIP_SETTLE:SKIP_SETTLE + n - SKIP_SETTLE]
    a, b = best_match_scale_offset(ref, meas)
    residual = [m - int(a * r + b) for r, m in zip(ref, meas)]
    return a, b, snr_db(ref, residual)


# ---------------------------------------------------------------------------
# Constant-input tests: easiest to reason about
# ---------------------------------------------------------------------------

class TestConstantInput:
    def test_zero_audio_gives_zero_demod(self):
        """All-zero audio in => modulated IQ is stationary (constant
        phase) => demod produces all zeros. Bit-exact."""
        audio_in = synthesize_audio_dc(200, 0)
        audio_out = _roundtrip(audio_in)
        # After settling, output should be exactly zero.
        steady = audio_out[SKIP_SETTLE:]
        assert all(v == 0 for v in steady), (
            f"zero-audio demod output should be all zero after settle; "
            f"first non-zero at index {next(i for i, v in enumerate(steady) if v != 0)} "
            f"with value {next(v for v in steady if v != 0)}")

    def test_dc_audio_gives_dc_demod(self):
        """Constant nonzero audio => constant frequency offset =>
        constant demod output (after settling). Not zero, not the
        input level, but constant."""
        audio_in = synthesize_audio_dc(200, 8000)
        audio_out = _roundtrip(audio_in)
        steady = audio_out[SKIP_SETTLE:]
        mean_steady = sum(steady) / len(steady)
        # All settled samples should be within a few LSB of the mean.
        max_dev = max(abs(v - mean_steady) for v in steady)
        assert max_dev < 5, (
            f"DC audio demod should settle to a constant; "
            f"max deviation from mean ({mean_steady:.1f}) was {max_dev}")
        # And the constant should be nonzero (sign tracking the input).
        assert mean_steady > 0, (
            f"positive DC audio should give positive DC demod; got {mean_steady:.1f}")

    def test_step_response_tracks_input(self):
        """Audio step from 0 to nonzero => demod transitions through
        an analogous step. Doesn't have to be a clean step (the chain
        is causal + low-passed), but the steady-state should differ
        between the two phases."""
        n = 400
        audio_in = synthesize_audio_step(n, level=6000, step_at=n // 2)
        audio_out = _roundtrip(audio_in)
        # Before step (skip initial settle): output should be ~zero.
        pre = audio_out[SKIP_SETTLE: n // 2 - 20]
        # After step (skip step transient): output should be nonzero +ve.
        post = audio_out[n // 2 + 60 :]
        pre_mean  = sum(pre) / len(pre)
        post_mean = sum(post) / len(post)
        assert abs(pre_mean) < 5, f"pre-step demod should be ~0, got {pre_mean:.1f}"
        assert post_mean > 20, f"post-step demod should be positive, got {post_mean:.1f}"


# ---------------------------------------------------------------------------
# Sinusoidal tones: SNR test
# ---------------------------------------------------------------------------

class TestTones:
    """SNR thresholds calibrated empirically. The chain recovers tones
    at audio frequencies well below the audio_decimator cutoff with
    high SNR; tones near the cutoff are heavily attenuated (so the
    recovered signal is small) but still recognisable.

    Frequencies are normalised — cycles per sample at the audio rate
    (post-decimation)."""

    @pytest.mark.parametrize("freq_norm,min_snr_db", [
        (0.001,  40.0),    # very low frequency
        (0.002,  35.0),
        (0.005,  25.0),
        (0.010,  20.0),
        (0.020,  18.0),    # higher; deeper into the FIR rolloff
    ])
    def test_tone_recovery_snr(self, freq_norm: float, min_snr_db: float):
        """A sinusoidal audio input should round-trip with at least
        ``min_snr_db`` after best-fit scale and offset."""
        n = 400
        audio_in = synthesize_audio_tone(n, amplitude=6000, freq_norm=freq_norm)
        audio_out = _roundtrip(audio_in)
        scale, offset, snr = _fit_snr(audio_in, audio_out)
        # Scale should be positive (phase coherence preserved).
        assert scale > 0, f"recovered tone has negative scale {scale:.4f} — phase inverted?"
        assert snr >= min_snr_db, (
            f"freq={freq_norm} tone roundtrip: SNR {snr:.1f} dB < {min_snr_db} dB; "
            f"scale={scale:.4f} offset={offset:.1f}")

    def test_multitone_recovery(self):
        """A two-tone audio should round-trip with both components
        recoverable. We check the composite SNR, which is the relevant
        metric for non-sinusoidal signals."""
        n = 500
        audio_in = synthesize_audio_multitone(n, [(3000, 0.002), (2500, 0.006)])
        audio_out = _roundtrip(audio_in)
        scale, offset, snr = _fit_snr(audio_in, audio_out)
        assert scale > 0
        assert snr >= 30.0, (
            f"multitone roundtrip SNR {snr:.1f} dB < 30 dB; "
            f"scale={scale:.4f} offset={offset:.1f}")


# ---------------------------------------------------------------------------
# Sanity checks on the helper functions themselves
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_audio_tone_amplitude(self):
        audio = synthesize_audio_tone(1000, amplitude=10000, freq_norm=0.01)
        assert max(audio) <= 10000
        assert min(audio) >= -10000
        # Should reach close to amplitude over many cycles.
        assert max(audio) > 9000
        assert min(audio) < -9000

    def test_upsample_ratio(self):
        from dv.fm_synthesis import upsample_hold
        out = upsample_hold([1, 2, 3], 4)
        assert out == [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3]

    def test_snr_inf_for_zero_noise(self):
        ref = [1, 2, 3, 4]
        assert snr_db(ref, [0, 0, 0, 0]) == float("inf")

    def test_snr_known(self):
        # 0 dB SNR: noise power == signal power.
        ref   = [100, -100, 100, -100]
        noise = [100, -100, 100, -100]
        assert abs(snr_db(ref, noise)) < 1e-9

    def test_best_fit_recovers_known_gain(self):
        ref = list(range(-50, 50))
        meas = [2 * r + 7 for r in ref]
        a, b = best_match_scale_offset(ref, meas)
        assert abs(a - 2.0) < 1e-9
        assert abs(b - 7.0) < 1e-9
