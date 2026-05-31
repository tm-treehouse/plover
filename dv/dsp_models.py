"""
Bit-exact Python reference models for the DSP units.

These models are the specification for the corresponding RTL: they perform
exactly the same integer arithmetic the hardware does, with the same bit
widths and the same wraparound (two's-complement modular) behaviour where
relevant. Tests compare RTL output to model output bit-for-bit.

Why "bit-exact" matters
-----------------------

A floating-point numpy reference (``np.convolve``, ``scipy.signal.lfilter``)
is great for design exploration but useless for verifying that a fixed-point
RTL implementation got the bit widths and truncation right. The whole point
of these models is to compute exactly what the hardware does — including
the parts of the hardware behaviour that "look wrong" but are correct (CIC
integrators *deliberately* wrap, FIR accumulators *deliberately* truncate
LSBs on output).

Q-format
--------

The RTL operates on plain signed integers. Q-interpretation (e.g. Q1.15
samples, Q1.15 coefficients) is a contract between the test and the
caller — it determines what input values represent and how to interpret
the output, but is invisible to the RTL itself. The models follow the
same convention: they take signed integers in and produce signed integers
out.

Modules
-------

* :class:`CicDecimator`    — N stages, decimation R, differential delay M.
* :class:`CicInterpolator` — N stages, interpolation R, differential delay M.
* :class:`FirFilter`       — Direct-form FIR with hot-updatable coefficients.
"""
from __future__ import annotations

import math


def _signed_wrap(value: int, width: int) -> int:
    """Wrap ``value`` into a signed ``width``-bit two's-complement range.

    The result is a Python int in the range ``[-2^(width-1), 2^(width-1))``.
    Used to model the inherent wrap behaviour of fixed-width registers in
    the RTL — for example CIC integrators are *expected* to wrap during
    normal operation; the comb stages subtract the wrap back out.
    """
    mask = (1 << width) - 1
    v = value & mask
    if v >> (width - 1):
        v -= (1 << width)
    return v


def _top_bits_signed(value: int, internal_width: int, out_width: int) -> int:
    """Take the top ``out_width`` bits of a signed ``internal_width``-bit
    value (arithmetic shift right). Returns a signed Python int.

    Models hardware ``out = internal[internal_width-1 -: out_width]``.
    """
    if internal_width <= out_width:
        # Sign-extend (no truncation needed). Return as-is.
        return _signed_wrap(value, max(internal_width, out_width))
    shift = internal_width - out_width
    # Arithmetic shift right on a signed Python int does the right thing.
    return _signed_wrap(value >> shift, out_width)


# ----------------------------------------------------------------------
# CIC decimator
# ----------------------------------------------------------------------

class CicDecimator:
    """Bit-exact model of a CIC decimator.

    The CIC structure: ``N`` integrators (running at the high/input rate),
    decimate by ``R``, then ``N`` comb stages with differential delay
    ``M`` (running at the low/output rate). Internal arithmetic uses
    ``INTERNAL_W = IN_W + ceil(log2((R*M)^N))`` bits — wide enough that
    the integrator wraparound is exactly recovered by the comb stages.

    The model holds internal state across :meth:`step` calls: call once
    per input sample. Returns the produced output sample (signed Python
    int in ``OUT_W`` bits) on cycles where decimation produces, ``None``
    otherwise.
    """

    def __init__(self, stages: int, decim: int, delay: int = 1,
                 in_w: int = 16, out_w: int = 16) -> None:
        assert stages >= 1
        assert decim >= 1
        assert delay >= 1
        self.N = stages
        self.R = decim
        self.M = delay
        self.IN_W = in_w
        self.OUT_W = out_w
        # Bit growth: ceil(log2((R*M)^N)). For power-of-2 R*M this is
        # exactly N * log2(R*M). For general R*M, slightly overprovisioned
        # (which is safe; just uses a few more bits than strictly needed).
        gain = (self.R * self.M) ** self.N
        self.GAIN_BITS = math.ceil(math.log2(gain)) if gain > 1 else 0
        self.INTERNAL_W = self.IN_W + self.GAIN_BITS
        # State
        self._integ = [0] * self.N
        self._comb_history: list[list[int]] = [
            [0] * self.M for _ in range(self.N)
        ]
        self._decim_cnt = 0

    def step(self, sample: int) -> int | None:
        """Push one input sample (signed, IN_W bits). Returns a signed
        OUT_W-bit output sample when decimation produces, else ``None``.

        Caller is responsible for clamping ``sample`` into the IN_W range
        (the RTL would just sign-extend it from a fixed-width port; the
        model is more strict so violations are visible).

        Pipelining note: this matches the RTL's *registered* integrator
        chain — each integrator's next value is computed from the
        current values of itself and its upstream neighbour, not from
        the just-computed new value. That means there are ``N-1`` cycles
        of pipeline delay through the integrators. The Python model
        snapshots ``self._integ`` before updating any stage, so each
        stage sees the prior-cycle values of its neighbours.
        """
        sample_ext = _signed_wrap(sample, self.IN_W)

        # Integrators — registered pipeline. Each stage uses the prior
        # value of itself and its upstream neighbour (matching the RTL's
        # sequential register updates).
        old_integ = list(self._integ)
        new_integ = [0] * self.N
        new_integ[0] = _signed_wrap(old_integ[0] + sample_ext, self.INTERNAL_W)
        for i in range(1, self.N):
            new_integ[i] = _signed_wrap(
                old_integ[i] + old_integ[i - 1], self.INTERNAL_W)
        self._integ = new_integ

        # Decimation
        self._decim_cnt += 1
        if self._decim_cnt < self.R:
            return None
        self._decim_cnt = 0

        # Comb stages — input is the (now updated) final integrator output.
        comb_in = self._integ[self.N - 1]
        for i in range(self.N):
            delayed = self._comb_history[i][-1]
            # Shift history: newest at index 0, oldest at end.
            self._comb_history[i] = [comb_in] + self._comb_history[i][:-1]
            comb_in = _signed_wrap(comb_in - delayed, self.INTERNAL_W)

        return _top_bits_signed(comb_in, self.INTERNAL_W, self.OUT_W)

    def run(self, samples: list[int]) -> list[int]:
        """Convenience: feed a list of input samples, return the list of
        decimated output samples (one output per R inputs)."""
        out = []
        for s in samples:
            r = self.step(s)
            if r is not None:
                out.append(r)
        return out


# ----------------------------------------------------------------------
# CIC interpolator
# ----------------------------------------------------------------------

class CicInterpolator:
    """Bit-exact model of a CIC interpolator.

    Reverse of the decimator: ``N`` comb stages first (at the low/input
    rate), then upsample by ``R`` (zero-stuff), then ``N`` integrators
    (at the high/output rate). Output is ``R`` samples per input sample.

    Call :meth:`step_in` per input sample. It returns a list of ``R``
    output samples (one full output frame per input). The output rate is
    fixed at exactly ``R`` per input.

    Bit growth here is the same shape as the decimator: the integrators
    produce values up to ``(R*M)^N`` larger than the input. Internal
    arithmetic at ``INTERNAL_W``.
    """

    def __init__(self, stages: int, interp: int, delay: int = 1,
                 in_w: int = 16, out_w: int = 16) -> None:
        assert stages >= 1
        assert interp >= 1
        assert delay >= 1
        self.N = stages
        self.R = interp
        self.M = delay
        self.IN_W = in_w
        self.OUT_W = out_w
        gain = (self.R * self.M) ** self.N
        self.GAIN_BITS = math.ceil(math.log2(gain)) if gain > 1 else 0
        self.INTERNAL_W = self.IN_W + self.GAIN_BITS
        # State
        self._comb_history: list[list[int]] = [
            [0] * self.M for _ in range(self.N)
        ]
        self._integ = [0] * self.N

    def step_in(self, sample: int) -> list[int]:
        """Push one input sample; produce ``R`` output samples.

        Pipelining note: the comb stages are a single-cycle
        combinational chain on each input handshake (the RTL implements
        them this way, which is acceptable at the low input rate). The
        integrators, however, are a pipelined register chain — each
        integrator uses prior-cycle upstream values, matching the RTL.
        We snapshot ``self._integ`` before updating any stage so each
        stage in the cascade sees the prior values.
        """
        sample_ext = _signed_wrap(sample, self.IN_W)

        # Comb stages — at input rate. Input goes through all N combs in
        # series; each subtracts its M-delayed value. Combinational
        # cascade (one cycle), so each stage uses the just-computed
        # upstream value.
        comb = sample_ext
        for i in range(self.N):
            delayed = self._comb_history[i][-1]
            self._comb_history[i] = [comb] + self._comb_history[i][:-1]
            comb = _signed_wrap(comb - delayed, self.INTERNAL_W)

        # Upsample: zero-stuff. The comb output enters the integrator chain
        # once; the next R-1 cycles see a zero input.
        out_samples = []
        for k in range(self.R):
            integ_in = comb if k == 0 else 0
            # Integrators — registered pipeline (each uses prior-cycle
            # upstream values, just like the decimator's integrators).
            old_integ = list(self._integ)
            new_integ = [0] * self.N
            new_integ[0] = _signed_wrap(
                old_integ[0] + integ_in, self.INTERNAL_W)
            for i in range(1, self.N):
                new_integ[i] = _signed_wrap(
                    old_integ[i] + old_integ[i - 1], self.INTERNAL_W)
            self._integ = new_integ
            out_samples.append(
                _top_bits_signed(self._integ[self.N - 1],
                                 self.INTERNAL_W, self.OUT_W))
        return out_samples

    def run(self, samples: list[int]) -> list[int]:
        """Feed a list of input samples, return the (R*len(samples))
        list of interpolated outputs."""
        out: list[int] = []
        for s in samples:
            out.extend(self.step_in(s))
        return out


# ----------------------------------------------------------------------
# FIR filter
# ----------------------------------------------------------------------

class FirFilter:
    """Bit-exact model of a direct-form FIR filter with hot-updatable
    coefficients.

    Architecture: a ``N_TAPS``-deep sample shift register and a
    ``N_TAPS``-entry coefficient memory. Each input sample produces one
    output sample, computed as the sum of element-wise products of the
    shift register and the coefficients.

    Hot-update: :meth:`set_coef` writes one coefficient by index; the new
    value affects the next call to :meth:`step`.

    Bit widths:
      * IN_W       — sample width
      * COEF_W     — coefficient width
      * Product    — IN_W + COEF_W bits
      * ACCUM_W    — IN_W + COEF_W + ceil(log2(N_TAPS)) bits (sum tree)
      * OUT_SHIFT  — right-shift before output truncation (lets caller
                      align Q-format; default COEF_W - 1, which preserves
                      Q1.(IN_W-1) input scale assuming Q1.(COEF_W-1) coefs)
      * OUT_W      — output width (top bits of shifted accumulator)
    """

    def __init__(self, n_taps: int, in_w: int = 16, coef_w: int = 16,
                 out_w: int | None = None, out_shift: int | None = None) -> None:
        assert n_taps >= 1
        self.N_TAPS = n_taps
        self.IN_W = in_w
        self.COEF_W = coef_w
        self.OUT_W = out_w if out_w is not None else in_w
        self.OUT_SHIFT = out_shift if out_shift is not None else (coef_w - 1)
        self.ACCUM_W = (
            in_w + coef_w + math.ceil(math.log2(max(n_taps, 2))))
        # State
        self._shift = [0] * self.N_TAPS
        self._coefs = [0] * self.N_TAPS

    def set_coef(self, index: int, value: int) -> None:
        """Set coefficient ``index`` to ``value`` (signed, COEF_W bits).
        Takes effect on the next :meth:`step` call."""
        assert 0 <= index < self.N_TAPS
        self._coefs[index] = _signed_wrap(value, self.COEF_W)

    def get_coef(self, index: int) -> int:
        return self._coefs[index]

    def step(self, sample: int) -> int:
        """Push one input sample, return one output sample.

        Mirrors the RTL: shift the new sample into the register, compute
        the sum of products with the current coefficient bank, shift
        right by OUT_SHIFT, truncate to OUT_W bits (taking the top
        OUT_W of the shifted value).
        """
        sample_ext = _signed_wrap(sample, self.IN_W)
        self._shift = [sample_ext] + self._shift[:-1]

        # Sum of products. Each product is IN_W + COEF_W bits signed.
        # Python ints are arbitrary precision so we just compute exactly.
        accum = 0
        for s, c in zip(self._shift, self._coefs):
            accum += s * c

        # Wrap to ACCUM_W bits (models a fixed-width accumulator's modulo
        # behaviour). For modest N_TAPS and reasonable coefficient values
        # this never wraps in practice, but the contract is "modular at
        # ACCUM_W bits" so we honour it.
        accum = _signed_wrap(accum, self.ACCUM_W)

        # Right-shift by OUT_SHIFT (drops LSBs to bring binary point back
        # to the input's Q position), then take top OUT_W bits.
        shifted = accum >> self.OUT_SHIFT
        # Truncate to OUT_W (top bits of what's left after the shift).
        # Match RTL: result = shifted[OUT_W-1:0] interpreted as signed.
        return _signed_wrap(shifted, self.OUT_W)

    def run(self, samples: list[int]) -> list[int]:
        return [self.step(s) for s in samples]
