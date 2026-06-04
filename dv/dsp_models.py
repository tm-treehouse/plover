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


class Nco:
    """Bit-exact model of a numerically-controlled oscillator.

    Produces (cos, sin) sample pairs from a phase accumulator that
    advances by a software-programmable increment each step. Matches
    ``units/nco/rtl/nco.sv`` exactly:

    * Phase accumulator is PHASE_W bits, unsigned, starts at 0.
    * Frequency = (phase_inc / 2^PHASE_W) * sample_rate.
    * The top LUT_N bits of the phase index a sin/cos lookup table.
    * Each LUT entry is round-half-up of (value * (2^(SAMPLE_W-1) - 1))
      with explicit `int(math.floor(x + 0.5))` for positives and
      `int(math.ceil(x - 0.5))` for negatives. This matches the RTL's
      $rtoi-based rounding.
    * Each step() advances phase by phase_inc, returns (cos, sin) for
      the *pre-advance* phase value (so the first sample at
      phase_inc=k is the cos/sin at phase=0, then phase=k, then
      phase=2k, ...).

    State:
        _phase_acc — current phase accumulator value
        _phase_inc — software-programmable increment, set via
                     :meth:`set_phase_inc`.
    """

    def __init__(self, sample_w: int = 16, phase_w: int = 32,
                 lut_n: int = 10,
                 sample_int_w:  int | None = None,
                 sample_frac_w: int | None = None) -> None:
        assert phase_w >= lut_n, (
            f"phase_w ({phase_w}) must be >= lut_n ({lut_n}); the top "
            "LUT_N bits of phase select the LUT entry.")
        self.SAMPLE_W = sample_w
        self.PHASE_W = phase_w
        self.LUT_N = lut_n
        self.LUT_SIZE = 1 << lut_n
        self.SAMPLE_INT_W  = sample_int_w  if sample_int_w  is not None else 1
        self.SAMPLE_FRAC_W = sample_frac_w if sample_frac_w is not None else (sample_w - self.SAMPLE_INT_W)
        assert self.SAMPLE_INT_W + self.SAMPLE_FRAC_W == self.SAMPLE_W
        # Maximum positive value in Q1.(SAMPLE_W-1) — matches the RTL's
        # LUT_SCALE = (1 << (SAMPLE_W-1)) - 1.
        self.LUT_SCALE = (1 << (sample_w - 1)) - 1
        self._build_luts()
        self._phase_acc = 0
        self._phase_inc = 0
        self._phase_mask = (1 << phase_w) - 1
        # Registered I/Q outputs. The RTL's i_data/q_data registers
        # are initialized to LUT_SCALE (cos(0)) and 0 (sin(0))
        # respectively after the first reset cycle (when the always
        # block fires due to !out_valid). The model mirrors that
        # initial state so beat 0 from both sides is (LUT_SCALE, 0).
        self._i_data = self.LUT_SCALE
        self._q_data = 0

    def _build_luts(self) -> None:
        """Populate sin_lut and cos_lut.

        Rounding policy must match the RTL exactly: round half-up
        (away from zero on negatives). RTL uses $rtoi after explicit
        +0.5 or -0.5 nudge. Python does the same with math.floor on
        positives, math.ceil on negatives — both reduce to "round
        half away from zero."
        """
        self.sin_lut: list[int] = []
        self.cos_lut: list[int] = []
        for k in range(self.LUT_SIZE):
            angle = 2.0 * math.pi * k / self.LUT_SIZE
            s = math.sin(angle) * self.LUT_SCALE
            c = math.cos(angle) * self.LUT_SCALE
            self.sin_lut.append(int(math.floor(s + 0.5)) if s >= 0
                                else int(math.ceil(s - 0.5)))
            self.cos_lut.append(int(math.floor(c + 0.5)) if c >= 0
                                else int(math.ceil(c - 0.5)))

    def set_phase_inc(self, value: int) -> None:
        self._phase_inc = value & self._phase_mask

    def get_phase_inc(self) -> int:
        return self._phase_inc

    def reset_phase(self) -> None:
        """Re-zero the phase accumulator. Useful when a test wants the
        first sample of its stream to be at phase=0 regardless of any
        prior usage."""
        self._phase_acc = 0

    def step(self) -> tuple[int, int]:
        """Produce one (cos, sin) pair at the current registered I/Q,
        then advance the phase and the registered I/Q.

        The RTL has a one-cycle pipeline lag between phase_acc and the
        registered I/Q outputs. The output sequence is:

            beat 0: i_data = cos_lut[0]                         (initial)
            beat 1: i_data = cos_lut[0]                         (first handshake captures cos[old phase_acc=0])
            beat 2: i_data = cos_lut[phase_inc >> shift]        (second handshake captures cos[phase_acc=phase_inc])
            beat 3: i_data = cos_lut[2*phase_inc >> shift]
            ...

        The model mirrors this by holding two pieces of state:

        * ``_phase_acc``  — the phase value that *will* index the LUT
                             on the *next* output beat (the RTL's
                             phase_acc register).
        * ``_i_data``, ``_q_data`` — the I/Q output of the *current*
                             beat (the RTL's i_data/q_data registers).

        On step(): return the current (_i_data, _q_data), then capture
        the new (_i_data, _q_data) from the current _phase_acc, then
        advance _phase_acc. This matches the RTL exactly because the
        clock-edge update of i_data uses lut_idx computed from the
        current (pre-advance) phase_acc, then the phase_acc itself
        advances on the same edge.
        """
        # Output the current registered values (held from prior step).
        out_i, out_q = self._i_data, self._q_data
        # Compute the new registered values from the *current* phase_acc
        # (these are what the RTL captures into i_data/q_data on the
        # clock edge of this handshake).
        idx = (self._phase_acc >> (self.PHASE_W - self.LUT_N)) & (self.LUT_SIZE - 1)
        self._i_data = self.cos_lut[idx]
        self._q_data = self.sin_lut[idx]
        # Advance phase_acc.
        self._phase_acc = (self._phase_acc + self._phase_inc) & self._phase_mask
        return (out_i, out_q)

    def run(self, n: int) -> list[tuple[int, int]]:
        """Produce ``n`` (cos, sin) pairs."""
        return [self.step() for _ in range(n)]



class ComplexMixer:
    """Bit-exact model of a complex multiplier (the "mixer" in an SDR
    receive/transmit chain).

    Multiplies two complex streams beat-by-beat:

        out = a * b
            = (I_a + j Q_a) * (I_b + j Q_b)
            = (I_a*I_b - Q_a*Q_b) + j * (I_a*Q_b + Q_a*I_b)

    All arithmetic operates on signed integers — Q-format is the
    consumer's responsibility (the model carries int_w/frac_w fields
    as informational). Output formation matches the RTL exactly: each
    sum/diff is arithmetic-right-shifted by ``out_shift`` (defaults to
    ``sample_frac_w``, preserving the input Q-position through the
    multiply), then the low ``sample_w`` bits taken as signed.

    State: none. The mixer is purely combinational from a model
    perspective — :meth:`step` takes two complex samples and returns
    one. The RTL has a one-cycle output register but the consumer's
    view of the per-beat math is identical.
    """

    def __init__(self, sample_w: int = 16,
                 sample_int_w:  int | None = None,
                 sample_frac_w: int | None = None,
                 out_shift:     int | None = None) -> None:
        self.SAMPLE_W = sample_w
        self.SAMPLE_INT_W  = sample_int_w  if sample_int_w  is not None else 1
        self.SAMPLE_FRAC_W = sample_frac_w if sample_frac_w is not None else (sample_w - self.SAMPLE_INT_W)
        assert self.SAMPLE_INT_W + self.SAMPLE_FRAC_W == self.SAMPLE_W
        self.OUT_SHIFT = out_shift if out_shift is not None else self.SAMPLE_FRAC_W

    def step(self, a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
        """One complex multiplication.

        ``a`` and ``b`` are ``(I, Q)`` signed integer tuples. Returns
        ``(I_out, Q_out)`` signed integers, after the arithmetic shift
        and low-bit truncation.
        """
        a_i, a_q = a
        b_i, b_q = b
        p_ii = a_i * b_i
        p_qq = a_q * b_q
        p_iq = a_i * b_q
        p_qi = a_q * b_i
        sum_i = p_ii - p_qq
        sum_q = p_iq + p_qi
        # Arithmetic right shift, then take low SAMPLE_W bits as signed
        # (matches RTL's shifted[SAMPLE_W-1:0]).
        out_i = _signed_wrap(sum_i >> self.OUT_SHIFT, self.SAMPLE_W)
        out_q = _signed_wrap(sum_q >> self.OUT_SHIFT, self.SAMPLE_W)
        return (out_i, out_q)

    def run(self, a_samples: list[tuple[int, int]],
            b_samples: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Run the mixer over paired sample lists. The two lists must
        have the same length; the output has the same length too."""
        assert len(a_samples) == len(b_samples), (
            f"complex_mixer.run: stream length mismatch "
            f"({len(a_samples)} vs {len(b_samples)})")
        return [self.step(a, b) for a, b in zip(a_samples, b_samples)]



class Agc:
    """Bit-exact model of the automatic gain control feedback loop.

    First closed-loop unit in the project. Match the RTL's gain
    register update timing precisely — any tiny disagreement in when
    `gain` is read versus written compounds into runaway divergence
    because the gain state's trajectory depends on its own history.

    Per-step contract (mirrors agc.sv exactly):

        magnitude = |in.I| + |in.Q|                          (cheap)
        error     = target - magnitude                       (signed)
        delta     = error >> mu_shift                        (signed)
        gain_next = clamp(gain + delta, gain_min, gain_max)  (clamped)
        out.I     = (in.I * gain) >> GAIN_FRAC_W             (uses CURRENT gain)
        out.Q     = (in.Q * gain) >> GAIN_FRAC_W
        # Then on the clock edge: gain <- gain_next.

    The output of step N is computed with the gain value that was in
    the register AT THE START of step N — i.e. the gain that was
    written at the end of step N-1. This matches the RTL where
    `out_i_q <= out_i_now` and `gain <= gain_next` happen on the same
    clock edge, but `out_i_now` is combinationally derived from the
    pre-edge value of `gain`.

    State:
        _gain — current gain register value (Q4.12 by default).

    Programming:
        :meth:`set_target`     — sets the AGC target magnitude
        :meth:`set_mu_shift`   — loop step = 2^-mu_shift
        :meth:`set_gain_clamp` — gain_min and gain_max
        :meth:`set_gain_init`  — gain at the next reset_gain pulse
        :meth:`reset_gain`     — sets gain = gain_init (analog of the
                                  RTL's control-register pulse)
    """

    def __init__(self, sample_w: int = 16,
                 sample_int_w:  int | None = None,
                 sample_frac_w: int | None = None,
                 gain_w: int = 16,
                 gain_int_w:    int | None = None,
                 gain_frac_w:   int | None = None) -> None:
        self.SAMPLE_W = sample_w
        self.SAMPLE_INT_W  = sample_int_w  if sample_int_w  is not None else 1
        self.SAMPLE_FRAC_W = sample_frac_w if sample_frac_w is not None else (sample_w - self.SAMPLE_INT_W)
        assert self.SAMPLE_INT_W + self.SAMPLE_FRAC_W == self.SAMPLE_W
        self.GAIN_W = gain_w
        self.GAIN_INT_W  = gain_int_w  if gain_int_w  is not None else 4
        self.GAIN_FRAC_W = gain_frac_w if gain_frac_w is not None else (gain_w - self.GAIN_INT_W)
        assert self.GAIN_INT_W + self.GAIN_FRAC_W == self.GAIN_W
        self.GAIN_DEFAULT = 1 << self.GAIN_FRAC_W   # 1.0 in Q-format

        # Register file mirrors. Match RTL reset values.
        self._target    = 0
        self._mu_shift  = 14
        self._gain_min  = 0
        self._gain_max  = (1 << self.GAIN_W) - 1
        self._gain_init = self.GAIN_DEFAULT
        # The actual feedback variable. RTL resets this to GAIN_DEFAULT,
        # not gain_init, because gain_init may not yet be programmed.
        self._gain      = self.GAIN_DEFAULT

    # ---- Register-bank-style programming API ----------------------------

    def set_target(self, value: int) -> None:
        self._target = _signed_wrap(value, self.SAMPLE_W)

    def set_mu_shift(self, value: int) -> None:
        assert 0 <= value < 32
        self._mu_shift = value

    def set_gain_clamp(self, gain_min: int, gain_max: int) -> None:
        mask = (1 << self.GAIN_W) - 1
        self._gain_min = gain_min & mask
        self._gain_max = gain_max & mask

    def set_gain_init(self, value: int) -> None:
        self._gain_init = value & ((1 << self.GAIN_W) - 1)

    def reset_gain(self) -> None:
        """Equivalent to writing 1 to the RTL's control-register pulse:
        copies gain_init into the gain register."""
        self._gain = self._gain_init

    def get_gain(self) -> int:
        return self._gain

    # ---- Datapath -------------------------------------------------------

    def step(self, in_iq: tuple[int, int]) -> tuple[int, int]:
        """One AGC update. ``in_iq`` is a signed ``(I, Q)`` tuple.
        Returns the gain-scaled ``(I, Q)`` output, then updates the
        internal gain register for the next step."""
        in_i, in_q = in_iq
        # 1. Output uses the CURRENT gain (pre-update).
        prod_i = self._gain * in_i
        prod_q = self._gain * in_q
        # Arithmetic right shift; Python's '>>' on signed ints is floor
        # (matches Verilator's '>>>').
        shifted_i = prod_i >> self.GAIN_FRAC_W
        shifted_q = prod_q >> self.GAIN_FRAC_W
        out_i = _signed_wrap(shifted_i, self.SAMPLE_W)
        out_q = _signed_wrap(shifted_q, self.SAMPLE_W)

        # 2. Update gain for next step (uses CURRENT input, same as RTL).
        magnitude = abs(in_i) + abs(in_q)
        error     = self._target - magnitude
        delta     = error >> self._mu_shift     # arithmetic shift (signed)
        gain_summed = self._gain + delta
        if gain_summed < self._gain_min:
            self._gain = self._gain_min
        elif gain_summed > self._gain_max:
            self._gain = self._gain_max
        else:
            self._gain = gain_summed

        return (out_i, out_q)

    def run(self, samples: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [self.step(s) for s in samples]



class Cordic:
    """Bit-exact model of the 16-stage vectoring CORDIC.

    Consumes a complex ``(I, Q)`` sample, produces ``(magnitude, phase)``.
    Magnitude is ``Kn * sqrt(I^2 + Q^2)`` where ``Kn ~ 1.6468`` is the
    well-known CORDIC gain (uncompensated by design — see RTL header
    comment). Phase is in signed ``PHASE_W``-bit two's complement,
    with ``+pi`` mapped to ``2^(PHASE_W-1) - 1`` and ``-pi`` mapped to
    ``-2^(PHASE_W-1)``.

    The model mirrors the RTL pipeline stage-by-stage. Each call to
    :meth:`step` performs the quadrant pre-rotation plus all 16
    iterations and returns the final ``(magnitude, phase)`` pair. No
    pipeline state is held between calls — the RTL's 16-cycle latency
    is a timing-only property; the per-sample math from input to
    output is the same.

    Critical for bit-exact agreement:

    * Python's ``>>`` on signed ints is arithmetic shift (floor
      semantics), matching Verilator's ``>>>`` on signed values.
    * The ATAN_LUT entries are generated with ``math.atan`` +
      ``math.floor(value + 0.5)`` for positives / ``math.ceil(value -
      0.5)`` for negatives — round-half-away-from-zero, matching the
      RTL's ``$rtoi(value +/- 0.5)`` pattern.
    * All intermediate ``x``, ``y`` values use ``INTERNAL_W = SAMPLE_W
      + 2`` bits signed, with ``_signed_wrap`` after each update to
      mirror RTL register truncation.
    """

    def __init__(self, sample_w: int = 16,
                 sample_int_w:  int | None = None,
                 sample_frac_w: int | None = None,
                 phase_w: int = 16,
                 iterations: int = 16) -> None:
        assert iterations == 16, (
            f"Cordic: only iterations=16 supported in v1 (got {iterations})")
        self.SAMPLE_W = sample_w
        self.SAMPLE_INT_W  = sample_int_w  if sample_int_w  is not None else 1
        self.SAMPLE_FRAC_W = sample_frac_w if sample_frac_w is not None else (sample_w - self.SAMPLE_INT_W)
        assert self.SAMPLE_INT_W + self.SAMPLE_FRAC_W == self.SAMPLE_W
        self.PHASE_W    = phase_w
        self.ITERATIONS = iterations
        self.INTERNAL_W = sample_w + 2

        # ATAN_LUT[k] = round(atan(2^-k) * 2^(PHASE_W-1) / pi)
        self.atan_lut: list[int] = []
        scale = (1 << (phase_w - 1)) / math.pi
        for k in range(iterations):
            angle = math.atan(2.0 ** (-k))
            value = angle * scale
            if value >= 0:
                rounded = int(math.floor(value + 0.5))
            else:
                rounded = int(math.ceil(value - 0.5))
            self.atan_lut.append(rounded)

        # pi/2 in the phase encoding.
        self.PI_OVER_2 = 1 << (phase_w - 2)
        self.NEG_PI_2  = -(1 << (phase_w - 2))

    def step(self, in_iq: tuple[int, int]) -> tuple[int, int]:
        """One CORDIC sample. Returns ``(magnitude, phase)``."""
        in_i, in_q = in_iq

        # ---- Quadrant pre-rotation ----
        if in_i >= 0:
            x, y, z = in_i, in_q, 0
        elif in_q >= 0:
            # Second quadrant: rotate by +pi/2.  (x, y) -> (y, -x).
            x, y, z = in_q, -in_i, self.PI_OVER_2
        else:
            # Third quadrant: rotate by -pi/2.  (x, y) -> (-y, x).
            x, y, z = -in_q, in_i, self.NEG_PI_2

        # Sign/zero-extend to INTERNAL_W and re-wrap to ensure the
        # representation matches the RTL register widths.
        x = _signed_wrap(x, self.INTERNAL_W)
        y = _signed_wrap(y, self.INTERNAL_W)
        z = _signed_wrap(z, self.PHASE_W)

        # ---- Iterations ----
        for k in range(self.ITERATIONS):
            # sigma = -sign(y). y < 0 => sigma = +1 => x_new = x - (y>>k).
            # y >= 0 => sigma = -1 => x_new = x + (y>>k).
            x_shifted = x >> k          # Python signed >> is arithmetic.
            y_shifted = y >> k
            atan_k    = self.atan_lut[k]
            if y < 0:
                new_x = x - y_shifted
                new_y = y + x_shifted
                new_z = z - atan_k
            else:
                new_x = x + y_shifted
                new_y = y - x_shifted
                new_z = z + atan_k
            x = _signed_wrap(new_x, self.INTERNAL_W)
            y = _signed_wrap(new_y, self.INTERNAL_W)
            z = _signed_wrap(new_z, self.PHASE_W)

        # ---- Output ----
        # Magnitude is the final x (which is unsigned after the iter
        # drives y -> 0). RTL stores it in INTERNAL_W bits signed but
        # the slice m_axis_tdata[INTERNAL_W-1:0] is the raw bit
        # pattern; downstream interprets as unsigned. The Python side
        # returns the signed value — the test harness reinterprets to
        # match the RTL packing.
        return (x, z)

    def run(self, samples: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [self.step(s) for s in samples]



class PhaseDiff:
    """Bit-exact model of the phase differentiator with implicit unwrap.

    Consumes a stream of signed PHASE_W-bit phase samples (in the
    NCO/CORDIC encoding: +-pi mapped to +-2^(PHASE_W-1)) and produces
    the per-sample frequency stream ``freq[n] = phase[n] - phase[n-1]``
    using signed-modular arithmetic. The PHASE_W-bit subtraction
    naturally handles +-pi wrap: a +pi-to--pi transition in phase
    produces a small positive frequency, not a large negative one,
    because the bit-width wrap cancels the 2*pi jump.

    State:
        _phase_prev — the most recently consumed phase sample. Reset
                      to 0; bit-exactness against the RTL requires
                      both sides start at 0.

    The first emitted output is ``phase[0] - 0 = phase[0]``, which
    isn't a meaningful frequency — it's just the absolute starting
    phase. Consumers should drop the first beat after reset, same as
    any settling pipeline.
    """

    def __init__(self, phase_w: int = 16) -> None:
        self.PHASE_W = phase_w
        self._phase_prev = 0

    def step(self, phase: int) -> int:
        """One phase sample in, one freq sample out."""
        # Signed-modular subtraction in PHASE_W bits. This is exactly
        # what the RTL's signed subtractor produces.
        diff = _signed_wrap(phase - self._phase_prev, self.PHASE_W)
        self._phase_prev = _signed_wrap(phase, self.PHASE_W)
        return diff

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
