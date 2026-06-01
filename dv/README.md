# `dv/` — shared DV components

Protocol agents and other test-infrastructure modules that more than
one unit uses. Imported as `dv.*` by the unit testbenches (and the top
testbench) — the project root is on `sys.path` via each test's
`HarnessConfig` setup.

The directory has four concerns:

* **Protocol agents** — `axi_lite_agent.py`, `axi_stream_agent.py`.
* **DSP reference models** — `dsp_models.py`.
* **Test plotting** — `dsp_plot.py`.
* **Public re-exports** — `__init__.py`.

## Protocol agents

`AxiLiteAgent` and `AxiStreamAgent` are pyuvm agents built on the
`pyuvm-dv-lib` (OpenTitan `dv_lib` port) base classes. Each agent is a
`(driver, monitor, sequencer)` triple wired up by `DVBaseAgent`'s
`build_phase`. Configuration is via `cfg` (per-agent), set on the env's
`agent_cfg` collection before agent construction.

**Active vs passive.** Both agents work in either mode via the base
`DVBaseAgent` logic keyed on `cfg.is_active`:

* `UVM_ACTIVE` — creates monitor + sequencer + driver. The default.
  Use when this agent drives stimulus.
* `UVM_PASSIVE` — creates only the monitor. Use to observe a bus
  without driving (e.g. monitoring a downstream xbar port from a
  top-level test).

**AxiLiteAgent.** Drives reads/writes via cocotbext-axi's `AxiLiteMaster`.
Lazily constructed on first use (`ensure_master()` exposes the BFM for
the firmware bridge — see `top/dv/firmware_bridge.py`). The monitor
samples AW/W/B and AR/R independently and publishes a completed
`AxiLiteItem` per transaction.

**AxiStreamAgent.** Role-aware: `cfg.role = "source"` (drives
`tvalid/tdata`) or `"sink"` (drives `tready=1` via an
`AxiStreamSink` BFM so a master-side AXIS port can transfer beats).
Sink BFM is built in `start_of_simulation_phase` so `tready` is asserted
from `t=0`. The monitor samples the bus and publishes per-beat items
regardless of role.

The agents are deliberately thin — most of the lifting is done by
cocotbext-axi. They exist to:

* Carry the per-transaction `Item` shape (op, addr, data, resp).
* Wire the cocotbext-axi BFM into pyuvm's `sequencer → driver` flow.
* Provide a *passive monitor* so the scoreboard sees the same bus
  events whether they came from a sequencer or from arbitrary other
  stimulus (e.g. the C firmware bridge driving the AxiLiteMaster
  directly).

## DSP reference models

`dsp_models.py` holds bit-exact Python implementations of every DSP
primitive in `units/`:

* `CicDecimator` — N integrators, decimate by R, N combs (delay M).
* `CicInterpolator` — N combs, zero-stuff upsample by R, N integrators.
* `FirFilter` — direct-form FIR with hot-update coefficient bank.
* `CicFirChain` — composition of `CicDecimator` + `FirFilter`, used
  by the project top's integration scoreboard.

These are *the spec* against which the RTL is verified. They perform
exactly the integer arithmetic the hardware does, including two's-
complement wrap on integrators, pipelined register-chain cascades,
and explicit truncation widths. When the bit-exact assertion fires,
the diagnostic points at the disagreement; nothing fuzzy about it.

Every signed signal has paired `*_int_w` / `*_frac_w` Q-format fields
mirroring the RTL parameters. Defaults are Q1.(W-1); explicit values
are checked for consistency in `__init__`. See the "Fixed-point
format" section in the project root `README.md` for the full
explanation.

## Test plotting

`dsp_plot.py` writes a three-panel comparison PNG per DSP test:

1. Input samples driven into the RTL.
2. Reference (model) and HDL output overlaid.
3. Diff (HDL − reference). Flat at zero when the test passed
   bit-exactly.

Plots are written to `<repo_root>/build/dsp_plots/<filename>.png`.
The unit tests call this from their test bodies; the top-level
integration tests call it from `PloverBaseTest._plot_chain()` at end
of run (before the assertion, so a failed test still produces a
diagnostic plot).

Matplotlib is the only dep, in the project's `dev` dependency group.
If it's not installed the helper silently skips — tests still
pass/fail on their bit-exact comparisons.

## Public re-exports

`__init__.py` re-exports the items most consumers import:

* `UVM_ACTIVE`, `UVM_PASSIVE` (from `dv_lib`)
* `AxiLiteOp`, `AxiLiteItem`, `AxiLiteAgentCfg`, `AxiLiteAgent`
* `AxiStreamItem`, `AxiStreamAgentCfg`, `AxiStreamAgent`

Driver/Monitor classes are *not* re-exported — nothing imports them
directly. Code that needs them (e.g. for a pyuvm factory override) can
still import from `dv.axi_lite_agent` / `dv.axi_stream_agent`.
