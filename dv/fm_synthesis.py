"""FM synthesis helpers — generate test signals for the FM receive chain.

This module is the inverse-direction counterpart to dv/dsp_models.py's
demod-side classes. It synthesises IQ baseband samples that a real FM
transmitter would produce given a known message signal, plus utility
helpers for the closed-loop tests:

  synthesize_audio(...)   - generate known audio test shapes
  apply_preemphasis(...)  - high-shelf boost (inverse of demod's deemphasis)
  upsample_hold(...)      - sample-and-hold upsampling (cheap, lossy)
  synthesize_fm_iq(...)   - integrate frequency to phase, emit IQ

This is *not* a faithful broadcast FM transmitter. It's a deterministic
IQ generator that closes the loop with FmDemodChain for testing purposes.
Specifically:

  * Upsampling is sample-and-hold — has sinc-shaped attenuation at higher
    audio frequencies. Adequate for low-frequency test tones; would need
    a real interpolation filter (CIC interpolator + FIR) for faithful
    broadcast emulation.

  * Pre-emphasis uses a single-pole IIR matching Deemphasis's shape.
    Correct for the project's chain, not necessarily matching any
    specific broadcast standard's exact response above the cutoff.

  * The carrier is at DC (baseband). Real broadcasts are translated up
    by the antenna front-end's mixer; that translation is not modelled
    here.

The acceptance criterion for closed-loop tests is recognisability and
SNR, not bit-exact agreement with the input audio. The various stages
(CORDIC truncation, IIR drift, CIC droop, FIR truncation, sample-and-
hold sinc) all introduce small error; the test SNR thresholds are
calibrated to a working loop, not perfection.
"""
from __future__ import annotations

import math
import random


# ---------------------------------------------------------------------------
# Audio test signal generators
# ---------------------------------------------------------------------------

def synthesize_audio_dc(n: int, level: int) -> list[int]:
    """Constant DC audio at ``level``."""
    return [level] * n


def synthesize_audio_step(n: int, level: int, step_at: int = 0) -> list[int]:
    """Zero for the first ``step_at`` samples, then constant ``level``."""
    return [0] * step_at + [level] * (n - step_at)


def synthesize_audio_tone(n: int, amplitude: int, freq_norm: float,
                          phase0: float = 0.0) -> list[int]:
    """Single sinusoid: ``amplitude * sin(2*pi*freq_norm*k + phase0)``.

    ``freq_norm`` is cycles per sample, in [0, 0.5). For an audio rate
    of 48 kS/s and a 1 kHz test tone, freq_norm = 1000/48000 ~ 0.0208.
    """
    return [int(amplitude * math.sin(2 * math.pi * freq_norm * k + phase0))
            for k in range(n)]


def synthesize_audio_multitone(n: int, components: list[tuple[int, float]]) -> list[int]:
    """Sum of sinusoids. Each entry of ``components`` is
    ``(amplitude, freq_norm)``."""
    out = [0] * n
    for amp, fn in components:
        for k in range(n):
            out[k] += int(amp * math.sin(2 * math.pi * fn * k))
    return out


# ---------------------------------------------------------------------------
# Pre-emphasis (inverse of the de-emphasis IIR)
# ---------------------------------------------------------------------------

def apply_preemphasis(audio: list[int], alpha: int, coef_frac_w: int = 15,
                      sample_w: int = 16) -> list[int]:
    """Apply a high-shelf boost that's the inverse of the Deemphasis
    low-pass: H_pre(z) * H_de(z) = 1 (in continuous-time approximation).

    Deemphasis has:    y[n] = (1-alpha) * x[n] + alpha * y[n-1]
    Pre-emphasis is:   y[n] = (1 / (1-alpha)) * (x[n] - alpha * x[n-1])

    The (1 / (1-alpha)) factor amplifies — significantly so for alpha
    near 1. For testing it's often easier to drop the gain factor and
    accept a constant gain mismatch (the loop will still be flat
    frequency-response; just offset in absolute level). Here we apply
    the *unscaled* pre-emphasis (x[n] - alpha*x[n-1]) because the
    closed-loop tests look at signal *shape*, not absolute level.

    Returns a list of signed ``sample_w``-bit integers.
    """
    max_v = (1 << (sample_w - 1)) - 1
    min_v = -(1 << (sample_w - 1))
    out: list[int] = []
    x_prev = 0
    max_coef = 1 << coef_frac_w
    one_minus_alpha = max_coef - alpha  # informational
    for x in audio:
        # Differentiator form: x[n] - alpha * x[n-1], scaled to fit.
        # The actual scaling (one_minus_alpha) is dropped — see docstring.
        # Clip to sample_w to prevent overflow.
        y = (max_coef * x - alpha * x_prev) >> coef_frac_w
        if y > max_v:
            y = max_v
        elif y < min_v:
            y = min_v
        out.append(y)
        x_prev = x
    return out


# ---------------------------------------------------------------------------
# Upsampling
# ---------------------------------------------------------------------------

def upsample_hold(samples: list[int], ratio: int) -> list[int]:
    """Sample-and-hold upsampling: each sample repeated ``ratio`` times.

    Cheap and lossy. Frequency response has sinc-shaped attenuation
    starting from DC; higher audio frequencies are attenuated. For
    low-frequency test tones (well below the upsampler's first null
    at fs_in/2) the attenuation is mild. A faithful broadcast emulator
    would use a polyphase interpolation filter; for project testing
    sample-and-hold is sufficient.
    """
    assert ratio >= 1
    out: list[int] = []
    for s in samples:
        out.extend([s] * ratio)
    return out


# ---------------------------------------------------------------------------
# FM modulation: integrate frequency to phase, emit IQ
# ---------------------------------------------------------------------------

def synthesize_fm_iq(frequencies: list[int], amp: int,
                     phase_w: int = 16, sample_w: int = 16,
                     phase0: int = 0) -> list[tuple[int, int]]:
    """Synthesise IQ from a per-sample frequency stream.

    ``frequencies[k]`` is the instantaneous frequency at sample k, in
    the same encoding as the demod chain's PhaseDiff output: signed
    ``phase_w``-bit, with +-Nyquist mapped to +-2^(phase_w-1).

    Algorithm:

        phase[k+1] = phase[k] + freq[k]        # modular in phase_w bits
        I[k] = amp * cos(phase[k] * pi / 2^(phase_w-1))
        Q[k] = amp * sin(phase[k] * pi / 2^(phase_w-1))

    The phase accumulator wraps modulo 2^phase_w (signed); the cos/sin
    are evaluated in floating-point and quantised to ``sample_w`` bits
    on output. This is the inverse of PhaseDiff in the demod direction:
    if you take this function's output, run it through Cordic + PhaseDiff,
    the recovered frequency should equal the input ``frequencies``
    (within CORDIC truncation noise, which is a few LSBs of phase_w).

    Returns a list of ``(I, Q)`` signed int tuples.
    """
    out: list[tuple[int, int]] = []
    phase = phase0 & ((1 << phase_w) - 1)
    phase_mask = (1 << phase_w) - 1
    phase_sign_bit = 1 << (phase_w - 1)
    # Scale factor: phase encoding (signed phase_w) -> radians
    rad_per_lsb = math.pi / (1 << (phase_w - 1))
    max_v = (1 << (sample_w - 1)) - 1
    for f in frequencies:
        # phase update in modular phase_w bits.
        phase = (phase + f) & phase_mask
        # Interpret as signed.
        sp = phase - (1 << phase_w) if phase & phase_sign_bit else phase
        theta = sp * rad_per_lsb
        i = int(amp * math.cos(theta))
        q = int(amp * math.sin(theta))
        # Clip to sample_w range (cos/sin can produce +amp; if amp is
        # near max, the floor in int() may slightly exceed max_v).
        if i > max_v: i = max_v
        if q > max_v: q = max_v
        out.append((i, q))
    return out


def message_to_frequencies(message: list[int], k_f: int,
                           message_sample_w: int = 16,
                           phase_w: int = 16) -> list[int]:
    """Convert a message signal to a per-sample frequency stream
    suitable for :func:`synthesize_fm_iq`.

    ``k_f`` is the peak frequency deviation in the phase encoding —
    when message reaches +-2^(message_sample_w-1), the output frequency
    reaches +-k_f. For FM broadcast (deviation 75 kHz, baseband
    2.4 MS/s, phase_w=16):

        freq_deviation_norm = 75000 / 2400000 = 0.03125 cycles/sample
        freq_deviation_lsb  = 0.03125 * 2^15 = 1024 lsb

    so k_f = 1024 would give realistic FM broadcast deviation.

    The conversion is just a linear scaling (no integration — that
    happens in synthesize_fm_iq).
    """
    msg_max = 1 << (message_sample_w - 1)
    return [(m * k_f) // msg_max for m in message]


# ---------------------------------------------------------------------------
# Closed-loop convenience
# ---------------------------------------------------------------------------

def synthesize_fm_iq_from_audio(audio: list[int], k_f: int,
                                 upsample_ratio: int,
                                 preemphasis_alpha: int | None = None,
                                 amp: int = 1 << 13,
                                 phase_w: int = 16,
                                 sample_w: int = 16) -> list[tuple[int, int]]:
    """End-to-end: take audio at the audio rate, apply optional
    pre-emphasis, sample-and-hold upsample to the baseband rate, and
    produce the IQ samples a transmitter would emit.

    This is the function the closed-loop tests will call. Pair its
    output with :class:`dv.dsp_models.FmDemodChain` to round-trip:

        iq    = synthesize_fm_iq_from_audio(audio, k_f, ratio, alpha)
        demod = FmDemodChain(...).run(iq)
        # demod should be recognisably the input audio.
    """
    if preemphasis_alpha is not None:
        audio = apply_preemphasis(audio, preemphasis_alpha, sample_w=sample_w)
    upsampled = upsample_hold(audio, upsample_ratio)
    freqs = message_to_frequencies(upsampled, k_f,
                                   message_sample_w=sample_w,
                                   phase_w=phase_w)
    return synthesize_fm_iq(freqs, amp=amp, phase_w=phase_w, sample_w=sample_w)


# ---------------------------------------------------------------------------
# Round-trip quality measurement
# ---------------------------------------------------------------------------

def snr_db(signal: list[int], noise: list[int]) -> float:
    """Compute signal-to-noise ratio in dB. ``signal`` is the reference,
    ``noise`` is (reference - measurement) — i.e. the residual after
    subtracting reference from measurement. Returns ``+inf`` if noise
    is identically zero."""
    assert len(signal) == len(noise)
    sig_power = sum(s * s for s in signal)
    noise_power = sum(n * n for n in noise)
    if noise_power == 0:
        return float("inf")
    if sig_power == 0:
        return float("-inf")
    return 10.0 * math.log10(sig_power / noise_power)


def best_match_scale_offset(reference: list[int], measurement: list[int]) -> tuple[float, float]:
    """Find scale ``a`` and offset ``b`` that minimise sum((a*ref + b - meas)^2).
    Returns ``(a, b)``. Useful for SNR computations when measurement
    has unknown gain/offset relative to reference."""
    n = len(reference)
    assert len(measurement) == n
    sx  = sum(reference)
    sy  = sum(measurement)
    sxx = sum(r * r for r in reference)
    sxy = sum(r * m for r, m in zip(reference, measurement))
    denom = n * sxx - sx * sx
    if denom == 0:
        return (0.0, sum(measurement) / n)
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return (a, b)
