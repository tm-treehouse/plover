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
                 in_w: int = 16, out_w: int = 16,
                 in_int_w: int | None = None,
                 in_frac_w: int | None = None,
                 out_int_w: int | None = None,
                 out_frac_w: int | None = None) -> None:
        assert stages >= 1
        assert decim >= 1
        assert delay >= 1
        self.N = stages
        self.R = decim
        self.M = delay
        self.IN_W = in_w
        self.OUT_W = out_w
        # Q-format fields. Default to Q1.(W-1) — matches every existing
        # test in the repo. Informational only: the model's arithmetic
        # is integer; these fields exist so tests/scoreboards can
        # describe the contract without re-deriving it from in_w/out_w.
        # If either of int_w / frac_w is given, both must be consistent
        # with the total (in_w/out_w). The default ((W-1) frac, 1 int)
        # is the typical signed-fractional layout.
        self.IN_INT_W   = in_int_w   if in_int_w   is not None else 1
        self.IN_FRAC_W  = in_frac_w  if in_frac_w  is not None else (in_w - self.IN_INT_W)
        self.OUT_INT_W  = out_int_w  if out_int_w  is not None else 1
        self.OUT_FRAC_W = out_frac_w if out_frac_w is not None else (out_w - self.OUT_INT_W)
        assert self.IN_INT_W  + self.IN_FRAC_W  == self.IN_W, (
            f"CicDecimator IN_INT_W ({self.IN_INT_W}) + IN_FRAC_W "
            f"({self.IN_FRAC_W}) != IN_W ({self.IN_W})")
        assert self.OUT_INT_W + self.OUT_FRAC_W == self.OUT_W, (
            f"CicDecimator OUT_INT_W ({self.OUT_INT_W}) + OUT_FRAC_W "
            f"({self.OUT_FRAC_W}) != OUT_W ({self.OUT_W})")
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
                 in_w: int = 16, out_w: int = 16,
                 in_int_w: int | None = None,
                 in_frac_w: int | None = None,
                 out_int_w: int | None = None,
                 out_frac_w: int | None = None) -> None:
        assert stages >= 1
        assert interp >= 1
        assert delay >= 1
        self.N = stages
        self.R = interp
        self.M = delay
        self.IN_W = in_w
        self.OUT_W = out_w
        # Q-format (informational; see CicDecimator).
        self.IN_INT_W   = in_int_w   if in_int_w   is not None else 1
        self.IN_FRAC_W  = in_frac_w  if in_frac_w  is not None else (in_w - self.IN_INT_W)
        self.OUT_INT_W  = out_int_w  if out_int_w  is not None else 1
        self.OUT_FRAC_W = out_frac_w if out_frac_w is not None else (out_w - self.OUT_INT_W)
        assert self.IN_INT_W  + self.IN_FRAC_W  == self.IN_W
        assert self.OUT_INT_W + self.OUT_FRAC_W == self.OUT_W
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
                 out_w: int | None = None, out_shift: int | None = None,
                 in_int_w:    int | None = None,
                 in_frac_w:   int | None = None,
                 coef_int_w:  int | None = None,
                 coef_frac_w: int | None = None,
                 out_int_w:   int | None = None,
                 out_frac_w:  int | None = None) -> None:
        assert n_taps >= 1
        self.N_TAPS = n_taps
        self.IN_W = in_w
        self.COEF_W = coef_w
        self.OUT_W = out_w if out_w is not None else in_w
        # Q-format fields (informational — like the CIC models, the FIR's
        # arithmetic operates on integers; these exist to document the
        # contract and to drive the default OUT_SHIFT below). Defaults
        # are Q1.(W-1) — matches every test in the repo.
        self.IN_INT_W    = in_int_w    if in_int_w    is not None else 1
        self.IN_FRAC_W   = in_frac_w   if in_frac_w   is not None else (in_w - self.IN_INT_W)
        self.COEF_INT_W  = coef_int_w  if coef_int_w  is not None else 1
        self.COEF_FRAC_W = coef_frac_w if coef_frac_w is not None else (coef_w - self.COEF_INT_W)
        self.OUT_INT_W   = out_int_w   if out_int_w   is not None else 1
        self.OUT_FRAC_W  = out_frac_w  if out_frac_w  is not None else (self.OUT_W - self.OUT_INT_W)
        assert self.IN_INT_W   + self.IN_FRAC_W   == self.IN_W
        assert self.COEF_INT_W + self.COEF_FRAC_W == self.COEF_W
        assert self.OUT_INT_W  + self.OUT_FRAC_W  == self.OUT_W
        # OUT_SHIFT defaults to COEF_FRAC_W so the multiply-and-accumulate
        # preserves the input's Q-position. When coefficients are
        # Q1.(COEF_W-1) the default works out to COEF_W-1, matching the
        # historical default.
        self.OUT_SHIFT = out_shift if out_shift is not None else self.COEF_FRAC_W
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

        # Right-shift by OUT_SHIFT (drops LSBs to bring binary point
        # back to the input's Q position), then take the LOW OUT_W
        # bits of the result. The low OUT_W bits after the shift hold
        # bits [OUT_SHIFT+OUT_W-1 : OUT_SHIFT] of the original
        # accumulator — so in effect OUT_W consecutive bits are
        # extracted starting at bit OUT_SHIFT.
        shifted = accum >> self.OUT_SHIFT
        # _signed_wrap takes the low OUT_W bits and interprets as
        # signed — matches the RTL's shifted[OUT_W-1:0]. Plain
        # truncation; no saturation, no rounding.
        return _signed_wrap(shifted, self.OUT_W)

    def run(self, samples: list[int]) -> list[int]:
        return [self.step(s) for s in samples]


class DcBlocker:
    """Bit-exact model of a first-order IIR DC blocker.

    Transfer function H(z) = (1 - z^-1) / (1 - alpha * z^-1). Removes
    the DC component of an input stream by placing a zero at DC and a
    pole at z = alpha (just inside the unit circle).

    Difference equation, matching the RTL exactly:

        y[n] = (x[n] - x[n-1]) + (alpha * y[n-1]) >> COEF_FRAC_W

    All arithmetic uses two's-complement wrap; the feedback product is
    shifted right by ``COEF_FRAC_W`` and the result is truncated to
    OUT_W bits (low bits of the shifted result, same convention as the
    FIR — no saturation, no rounding).

    Honest caveat: truncation in a feedback path drifts toward negative
    values over time (truncation rounds toward -infinity). Documented
    in the RTL and inherited here. A rounding variant could be added
    later if a chain shows measurable bias.

    The coefficient bank is one register (``alpha``), updated via
    :meth:`set_alpha`. Defaults to 0 — the filter acts as a pure
    differentiator y[n] = x[n] - x[n-1] until alpha is programmed.
    """

    def __init__(self, in_w: int = 16, coef_w: int = 16,
                 out_w: int | None = None,
                 in_int_w:    int | None = None,
                 in_frac_w:   int | None = None,
                 coef_int_w:  int | None = None,
                 coef_frac_w: int | None = None,
                 out_int_w:   int | None = None,
                 out_frac_w:  int | None = None) -> None:
        self.IN_W = in_w
        self.COEF_W = coef_w
        self.OUT_W = out_w if out_w is not None else in_w
        # Q-format (informational; defaults to Q1.(W-1) per the
        # project convention).
        self.IN_INT_W    = in_int_w    if in_int_w    is not None else 1
        self.IN_FRAC_W   = in_frac_w   if in_frac_w   is not None else (in_w - self.IN_INT_W)
        self.COEF_INT_W  = coef_int_w  if coef_int_w  is not None else 1
        self.COEF_FRAC_W = coef_frac_w if coef_frac_w is not None else (coef_w - self.COEF_INT_W)
        self.OUT_INT_W   = out_int_w   if out_int_w   is not None else 1
        self.OUT_FRAC_W  = out_frac_w  if out_frac_w  is not None else (self.OUT_W - self.OUT_INT_W)
        assert self.IN_INT_W   + self.IN_FRAC_W   == self.IN_W
        assert self.COEF_INT_W + self.COEF_FRAC_W == self.COEF_W
        assert self.OUT_INT_W  + self.OUT_FRAC_W  == self.OUT_W
        # State
        self._x_prev = 0
        self._y_prev = 0
        self._alpha = 0

    def set_alpha(self, value: int) -> None:
        """Program the feedback coefficient. ``value`` is signed,
        interpreted in Q-format. Stored truncated to COEF_W bits."""
        self._alpha = _signed_wrap(value, self.COEF_W)

    def get_alpha(self) -> int:
        return self._alpha

    def step(self, sample: int) -> int:
        """Push one input sample; return one output sample.

        Matches the RTL's combinational expression and registered
        output exactly: y_next = (x - x_prev) + (alpha*y_prev) >> COEF_FRAC_W,
        truncated to OUT_W. State (x_prev, y_prev) updates after each
        call.
        """
        x = _signed_wrap(sample, self.IN_W)
        diff = x - self._x_prev
        # Product width = COEF_W + OUT_W (matches the RTL's PROD_W).
        # The feedback product can hold its full bit growth; we then
        # arithmetic-right-shift by COEF_FRAC_W (drops fractional bits
        # of the product so it returns to OUT_W's Q-position) and
        # truncate to OUT_W low bits.
        prod = self._alpha * self._y_prev
        # Python int >> on a negative value is arithmetic (sign-extending)
        # which matches Verilog >>> on signed values.
        shifted = prod >> self.COEF_FRAC_W
        feedback = _signed_wrap(shifted, self.OUT_W)
        # diff is wider than OUT_W; take low OUT_W bits, signed. This
        # mirrors the RTL's diff_in_out_w = diff_term[OUT_W-1:0].
        diff_trunc = _signed_wrap(diff, self.OUT_W)
        y_next = _signed_wrap(diff_trunc + feedback, self.OUT_W)
        # Update state for the next call.
        self._x_prev = x
        self._y_prev = y_next
        return y_next

    def run(self, samples: list[int]) -> list[int]:
        return [self.step(s) for s in samples]


# ----------------------------------------------------------------------
# System-level chain models
# ----------------------------------------------------------------------

class CicFirChain:
    """Bit-exact reference for the integrated CIC-decimator -> FIR chain
    used inside ``plover.sv``.

    Composition rather than reimplementation: this wraps a
    :class:`CicDecimator` followed by a :class:`FirFilter` and chains
    them sample-by-sample. The CIC produces one output every R inputs;
    those outputs feed the FIR one-for-one. So pushing N input samples
    yields ``N // R`` FIR output samples (modulo the FIR's own one-cycle
    pipeline fill, which is exactly mirrored in the RTL).

    The FIR's coefficient bank is hot-updatable via :meth:`set_coef`; the
    test wraps an AXI-Lite write to the FIR base in software and calls
    :meth:`set_coef` on the model in lock-step so both stay in sync.

    Why a system-level model is its own class:
        * The chain's output is what the testbench actually sees on the
          top-level AXIS output port. Composing the two primitives here
          means tests don't have to reach into the chain's intermediate
          signals to predict — they just compare the model's chain
          output against the RTL's chain output.
        * The bit-exactness contract is inherited: each primitive is
          bit-exact against its RTL, so the chain is bit-exact against
          its RTL.
        * Future system-level scoreboards can ask one object for "what
          should the chain produce given these inputs and this coef
          set?" — same shape as a register-level scoreboard's
          ``predict()`` for the control units.
    """

    def __init__(self,
                 cic_stages: int = 3,
                 cic_decim:  int = 4,
                 cic_delay:  int = 1,
                 cic_in_w:   int = 16,
                 cic_out_w:  int = 16,
                 fir_n_taps: int = 8,
                 fir_in_w:   int | None = None,
                 fir_coef_w: int = 16,
                 fir_out_w:  int = 16,
                 fir_out_shift: int | None = None,
                 # Q-format. All optional; defaults are Q1.(W-1) for
                 # each width.
                 sample_int_w:    int | None = None,
                 sample_frac_w:   int | None = None,
                 fir_coef_int_w:  int | None = None,
                 fir_coef_frac_w: int | None = None) -> None:
        if fir_in_w is None:
            fir_in_w = cic_out_w
        # OUT_SHIFT default tracks the coefficient fractional-bit count
        # when one is provided; otherwise falls back to the historical
        # COEF_W-1 (i.e. Q1.(COEF_W-1)).
        if fir_out_shift is None:
            if fir_coef_frac_w is not None:
                fir_out_shift = fir_coef_frac_w
            else:
                fir_out_shift = fir_coef_w - 1
        assert fir_in_w == cic_out_w, (
            f"chain stage widths must match: CIC output is {cic_out_w} bits, "
            f"FIR input declared {fir_in_w} bits")
        # Same Q-format for CIC in/out and FIR in/out — the chain is
        # SAMPLE_W-wide end-to-end with one Q-position.
        self.cic = CicDecimator(
            stages=cic_stages, decim=cic_decim, delay=cic_delay,
            in_w=cic_in_w,  out_w=cic_out_w,
            in_int_w=sample_int_w,   in_frac_w=sample_frac_w,
            out_int_w=sample_int_w,  out_frac_w=sample_frac_w,
        )
        self.fir = FirFilter(
            n_taps=fir_n_taps, in_w=fir_in_w,
            coef_w=fir_coef_w, out_w=fir_out_w,
            out_shift=fir_out_shift,
            in_int_w=sample_int_w,    in_frac_w=sample_frac_w,
            coef_int_w=fir_coef_int_w, coef_frac_w=fir_coef_frac_w,
            out_int_w=sample_int_w,   out_frac_w=sample_frac_w,
        )

    # ---- Coefficient bank (proxy to the underlying FIR) ----
    def set_coef(self, index: int, value: int) -> None:
        self.fir.set_coef(index, value)

    def get_coef(self, index: int) -> int:
        return self.fir.get_coef(index)

    # ---- Step / run ----
    def step(self, sample: int) -> int | None:
        """Push one input sample at the chain input rate. Returns the
        chain's output sample on cycles where the CIC produces (i.e.
        every R inputs); ``None`` otherwise."""
        cic_out = self.cic.step(sample)
        if cic_out is None:
            return None
        return self.fir.step(cic_out)

    def run(self, samples: list[int]) -> list[int]:
        """Convenience: feed a list of input samples, return the list
        of chain-output samples (one output per R inputs)."""
        out = []
        for s in samples:
            r = self.step(s)
            if r is not None:
                out.append(r)
        return out
