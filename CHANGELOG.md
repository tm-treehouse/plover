# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims
to follow semantic versioning.

## [Unreleased]

### Added
- **Fixed-point Q-format parameters on every DSP unit and the top.**
  Each signed signal (sample input, sample output, FIR coefficient)
  now has paired `*_INT_W` and `*_FRAC_W` parameters next to its
  existing total `*_W`. The three are related by
  `*_W = *_INT_W + *_FRAC_W` and an elaboration-time `$fatal`
  assertion catches mismatches.

  The parameters are *informational* with respect to arithmetic —
  every unit still operates on plain signed integers internally. Their
  job is to make the Q-position legible at the instantiation site
  (you don't have to reverse-engineer it from `IN_W` and `OUT_SHIFT`)
  and to let elaboration catch silly mistakes (passing inconsistent
  totals/splits).

  One real effect: the FIR's `OUT_SHIFT` default changes from
  `COEF_W-1` to `COEF_FRAC_W`. Identical value with Q1.(W-1) defaults
  (`COEF_FRAC_W` defaults to `COEF_W-1`), but the *name* now reflects
  the meaning: "shift by the coefficient's fractional-bit count to
  preserve the input's Q-position through the multiply." Setting
  `COEF_INT_W=3, COEF_FRAC_W=13` (Q3.13 coefs) makes `OUT_SHIFT`
  default to 13 — the right thing without users having to know to
  compute it.

  Defaults across all units are Q1.(W-1) — the format every existing
  test already used — so the change is backward-compatible. Every
  existing instantiation (passing only `IN_W=16, COEF_W=16, OUT_W=16`)
  works unchanged.

  Mirrored in the Python models: `CicDecimator`, `CicInterpolator`,
  `FirFilter`, and `CicFirChain` all gained `*_int_w` / `*_frac_w`
  fields with the same defaults and assertion. The `fir_filter` pytest
  config grew a fourth parameter sweep row exercising Q3.13
  coefficients end-to-end: both the RTL and the Python model are
  configured with `COEF_INT_W=3, COEF_FRAC_W=13, OUT_SHIFT=13` and
  bit-exact agreement is confirmed across all four test scenarios
  (impulse, averaging, arbitrary, hot_update). 16 FIR tests now (was
  12).

  README gained a "Fixed-point format" section explaining the
  parameter set with a worked example.

- **CIC -> FIR signal chain integrated into `plover.sv`.** The DSP
  units verified standalone in the previous arc now form an inline
  signal chain at the project top:

      s_axis_* (16-bit signed, in rate)
        -> cic_decimator (N=3, R=4, M=1)
        -> fir_filter   (8 taps, hot-update via AXI-Lite)
        -> m_axis_* (16-bit signed, out rate = in / R)

  `stream_sink` is no longer instantiated at the top — its `s_axis_*`
  port is repurposed as the chain input, and a new `m_axis_*` port
  carries the chain output. The xbar gains a third slave: `fir_filter`
  at page `0x0000_2000`, hosting the coefficient bank (byte offset
  `4*i` selects tap `i`). The `stream_sink` unit itself stays in
  `units/` as a verified-but-no-longer-used template; its unit DV
  still runs.

- **`CicFirChain` system-level Python reference model in
  `dv/dsp_models.py`.** Composes the existing `CicDecimator` +
  `FirFilter` primitives sample-by-sample; same bit-exactness contract
  as the underlying models. The integration scoreboard uses this as
  the chain's ground truth: predict per input sample, compare per
  output sample.

- **DSP-aware scoreboard in `top/dv/plover_env.py`.** Maintains a
  `CicFirChain` model and keeps it in lock-step with the RTL via three
  bus-observation streams:
  1. AXI-Lite monitor → watches writes to the FIR page, forwards them
     to `model.set_coef()`. Works for sequencer-driven AND
     C-firmware-driven coefficient updates equally — the passive bus
     monitor sees both as ordinary bus events. This is the
     OpenTitan-style invariant the project's agent design buys.
  2. AXIS-in monitor → feeds each observed input sample to
     `model.step()`, queuing the model's predicted outputs.
  3. AXIS-out monitor → compares each observed chain output against
     the next queued prediction. Mismatches are logged with
     index/expected/got; end-of-test assertion is `len(mismatches) == 0`.

- **`AxiStreamAgent` is now role-aware (source / sink).** New
  `cfg.role` field — default `"source"` (drives `tvalid/tdata`),
  alternative `"sink"` (instantiates a cocotbext-axi `AxiStreamSink`
  that drives `tready=1` so the DUT's master AXIS port can transfer
  beats). Needed for the chain's `m_axis_*` output port; the sink
  BFM is built in `start_of_simulation_phase` so `tready` is asserted
  from `t=0`. Backward-compatible: existing tests that don't set
  `role` default to `"source"` and behave as before.

- **`plover_program_fir` C firmware function** in `top/host/`,
  reachable via the firmware bridge's new `run_program_fir`. Walks a
  caller-supplied coefficient table and writes each tap to
  `fir_base + 4*i`; optionally reads each tap back to confirm. The C
  is standalone — no peakrdl-generated headers (the FIR's bank is
  memory-style, not RDL-described). Used by the
  `firmware_program_fir` integration test that programs an averaging
  filter from C and confirms the chain output bit-exactly.

- **DSP-aware top tests.** Two new "signal-carrying" tests:
  - `chain_impulse` — programs the FIR as a delta filter and drives
    an impulse stream; scoreboard verifies the chain output is the
    bit-exact CIC impulse response.
  - `chain_tone` — programs the FIR as a unity-gain averager and
    drives a sinusoidal stream parameterised by `freq_norm`,
    `amplitude_frac`, `num_inputs`. Sequence carries the signal
    info; adding more signal types (chirp, multi-tone, noise) is a
    small addition.

  Plus `firmware_program_fir` (the C path version) and an updated
  `smoke` test that no longer touches the dropped `sink_*` signals.
  Top test count is now 5 (was 3).

- **FIR address decoding masks the page-base bits.** The standalone
  FIR's AXI-Lite slave previously used `aw_addr_q[31:2]` as the word
  index, which worked when the test drove addresses in
  `[0, N_TAPS*4)` (high bits zero) but failed once the xbar started
  delivering full bus addresses like `0x0000_2000 + 4*i`. Now masks
  the captured address to `ADDR_BITS = 2 + ceil(log2(N_TAPS))` bits
  before decoding. Standalone FIR DV unchanged in behaviour;
  integration works correctly. Bug-injection confirmed: tying the
  CIC→FIR data wire to zero produces "63 mismatch(es); first: idx=0
  expected=9 got=0"; breaking the FIR's address routing produces
  "62 mismatch(es)" on `chain_tone`. The scoreboard catches
  integration regressions the unit DVs cannot (each unit works in
  isolation; the chain test catches wiring bugs between them).

- **`axil_shell.CONTROL` bits exposed as ports + integration use.**
  `axil_shell` now drives two new combinational outputs continuously
  from `reg_control`: `control_enable` (bit 0 = `CONTROL.ENABLE`) and
  `control_spare[30:0]` (bits 31:1 = `CONTROL.SPARE`). In `plover.sv`
  these are plumbed to gate the counter, so software writes to
  `CONTROL.ENABLE` actually start/stop the count. The integration
  smoke test exercises the full cycle: counter held at 0 after reset
  (ENABLE=0 default), write ENABLE=1 → counter advances ten cycles in
  ten clocks, write ENABLE=0 → counter freezes for ten cycles
  unchanged. New `axil_shell` unit test `control_ports` directly
  drives CONTROL and samples the port outputs to confirm bit-for-bit
  fidelity; bug-injection verified (tying `control_enable` high
  produces "control_enable port: wrote CONTROL=0x00000000, expected
  enable=0, got 1").
- **Passive-only agent mode documented and exposed.** Both
  `AxiLiteAgent` and `AxiStreamAgent` already supported `UVM_PASSIVE`
  via the inherited `DVBaseAgent.build_phase` logic (monitor only, no
  driver, no sequencer); the `dv/` package now re-exports
  `UVM_ACTIVE` / `UVM_PASSIVE` so testbenches can do
  `from dv import UVM_PASSIVE` alongside the agent classes. Module
  docstring documents the use case (observing an external master
  whose stimulus this DUT doesn't drive).
- **Project-local `dv/` package with shared protocol agents.** New
  top-level `dv/` (peer to `units/`, `top/`, `tools/`) holds protocol-
  specific DV components that the upstream `pyuvm-dv-lib` doesn't ship:
  `AxiLiteAgent` (item / cfg / driver / monitor / agent) for AXI-Lite
  stimulus, `AxiStreamAgent` for AXI-Stream stimulus. The shape mirrors
  OpenTitan's split between `dv_lib` (base classes — upstream) and
  `cip_lib` (protocol agents — in-project, since `cip_lib` isn't ported
  in pyuvm-dv-lib). Both agents extend the dv_lib base classes and use
  cocotbext-axi BFMs underneath.
- **Shared pytest harness at `tools/dv_harness.py`.** A single module
  owns FuseSoC `--setup` invocation, EDAM parsing, source-resolution
  back to live RTL, Verilator-from-wheel hookup, and the
  `cocotb_tools.runner` build/test flow. Each unit's
  `test_<unit>_pytest.py` is now a ~30-line shim that constructs a
  `HarnessConfig` and calls `run_testcase()`. Replaces six divergent
  copies (~840 lines total) of the same logic with one shared module
  (~270 lines) plus tiny shims (~240 lines) — net ~330 lines saved, and
  no more drift potential between subtly different harnesses.

### Changed
- **AXI agents handle reset cleanly.** The `AxiLiteMonitor` and
  `AxiStreamMonitor` now check the configured reset signal each
  cycle. While reset is asserted, AxiLite flushes its internal AW/W/
  AR pairing queues (so a partially-observed transaction that gets
  killed by a mid-flight reset doesn't pair against unrelated
  post-reset traffic), and AxiStream skips sampling (no internal
  state to flush — it just doesn't emit beats for X-or-mid-reset
  signal values). No current test exercises mid-transaction reset,
  but the defensive code is in place for future tests that do.
- **All unit testbenches now use the pyuvm-dv-lib framework.**
  Previously three units (`stream_sink`, `axil_xbar`, and the project
  `top`) bypassed the dv_lib base classes and brought up cocotbext-axi
  BFMs inline. They now follow the same env/agent/scoreboard/test
  pattern as `axil_shell` / `counter` / `syscon`:
  - `stream_sink` gets `stream_sink_env.py` (reference model that tracks
    expected `beat_count`/`data_xor`) + `stream_sink_test.py` (vseq +
    base test with end-of-run sampling).
  - `axil_xbar` gets `axil_xbar_env.py` (routing + RAM-stub reference
    model that predicts response/data for every transaction) +
    `axil_xbar_test.py` (three vseqs — smoke / decerr / concurrent).
  - The project top gets `plover_env.py` (two-agent env with AXIS-side
    scoreboard) + `plover_test.py` (three vseqs, firmware path bypasses
    the sequencer to call into the C bridge directly).
  Bug-injection verified all three new scoreboards have teeth: an RTL
  XOR-by-zero in stream_sink produces `data_xor mismatch: got
  0x00000000, expected 0x3366176b`; drifting the xbar's reference-model
  address bases produces `6 scoreboard mismatch(es)`.
- **Shared `AxiLiteAgent` replaces per-unit duplicates.** `axil_shell`
  and `syscon` previously each had their own `axil_agent.py` /
  `syscon_agent.py` — ~270 lines of nearly identical code. Both now
  import from `dv.axi_lite_agent` and the unit-local copies are
  deleted. Class names normalized from `Axil*` / `Syscon*` (which
  conflated protocol with unit) to `AxiLite*` (protocol-named).
- **Both shared agents now use OpenTitan-style passive bus monitors,
  not driver mirrors.** `AxiLiteMonitor` and `AxiStreamMonitor` sample
  the configured bus directly each cycle (RisingEdge + ReadOnly), detect
  handshakes (VALID && READY), reconstruct transactions, and publish
  via the analysis port. The drivers no longer mirror to the monitor
  (they still back-annotate `item.resp`/`item.data` so sequences can
  read the results after `finish_item`, but they don't write to any
  analysis port). This matches the OpenTitan dv_lib invariant: the
  scoreboard's view of what happened comes from the bus, not from any
  one source of stimulus. The concrete value-add is on the project top:
  the `firmware_smoke` / `firmware_concurrent` tests issue AXI-Lite
  transactions from C via the bridge, bypassing the sequencer; with
  the new monitor the scoreboard sees those transactions ("observed 2
  AXI-Lite transaction(s) on the bus" after `firmware_smoke` reads
  shell.ID + syscon.VERSION), whereas the driver-mirror would have
  shown zero. AxiLite monitor splits read and write paths into
  independent coroutines so both can be in flight simultaneously
  without interfering.
- **`units/axil_xbar/` — AXI4-Lite 1-to-N decoder with optional register
  stages.** New sub-unit at the usual `units/<name>/` shape. The decoder
  routes each AXI-Lite transaction to one of N downstream slaves based on
  per-slave `SLAVE_BASE`/`SLAVE_MASK` parameter arrays (4 KB pages are
  the natural choice; the unit DV uses `0x0000_0000` and `0x0000_1000`).
  Unmapped addresses return `RESP_DECERR` (`2'b11`). Two small FSMs
  (write side: `W_IDLE` → `W_DATA` → `W_RESP`; read side: `R_IDLE` →
  `R_RESP`) hold the in-flight target per channel so AW/W decode results
  persist until the matching B/R returns; read and write paths are fully
  independent so a read to slave A can be in flight while a write to
  slave B is in flight. Optional `INPUT_REG_STAGES` / `OUTPUT_REG_STAGES`
  parameters insert AXI-Lite-compliant skid buffers on the master-side
  and per-slave channels for timing closure (default 0 = combinational).
  A small helper module `axil_skid_buffer` supports `DEPTH={0,1,N}`.
  Standalone DV at `units/axil_xbar/dv/`: a tiny `axil_ram_stub`
  behavioural slave + `axil_xbar_dv_top` wrapper that exposes one master
  port and runs three test cases (`smoke` — routing isolation,
  `decerr` — unmapped → DECERR + recovery, `concurrent` — independent
  R+W) across three stage configurations `(0,0)`, `(1,0)`, `(0,1)`, for
  nine parametrized runs.
- **plover top consolidated to a single host-side AXI-Lite port.**
  `plover.sv` now exposes one `s_axil_*` slave (32-bit address, 32-bit
  data) instead of the previous parallel `s_axil_*` + `s_syscon_*`. An
  `axil_xbar` inside the top fans the unified bus out to `axil_shell`
  (page `0x0000_0000`) and `syscon` (page `0x0000_1000`); the
  peripherals keep their 8-bit AWADDR/ARADDR (decoder strips upper bits
  when forwarding, which avoids touching peripheral RTL). Two new
  parameters `XBAR_INPUT_REG_STAGES` / `XBAR_OUTPUT_REG_STAGES` (default
  0) let the integrator dial in register stages without code edits.
  `top/plover.core` adds `::axil_xbar` to its dependencies.
- **Host firmware and bridge collapsed to one bus.** `plover_host_ops`
  is now a single `read`/`write` callback pair (was four: shell_*/
  syscon_*). `plover_hello_world` takes `shell_base` and `syscon_base`
  arguments so the firmware can compute absolute addresses on the
  unified bus. The C uses a new `REG_ADDR(base, type, field)` helper
  that combines the host-supplied page base with `offsetof(type, field)`
  from the peakrdl-cheader output. The Python bridge
  (`firmware_bridge.py`) shrinks correspondingly: one `AxiLiteMaster`,
  three callbacks (read/write/log), and `run_hello_world` takes the
  master plus keyword-only `shell_base`/`syscon_base`/
  `expected_syscon_version`/`include_dirs`.
- **Integration `smoke` test exercises DECERR.** The top's smoke test
  now writes and reads an unmapped address (`0x0000_2000`) and confirms
  both return `RESP_DECERR`. Bug-injection (swapping the xbar's
  `SLAVE_BASE` array) fails the smoke test loudly with a clean
  diagnostic (`axil_shell.ID via xbar: got 0x00000001, expected
  0xc0c07b01`).
- `top/` project top sitting parallel to `units/`: top-level RTL
  (`top/rtl/plover.sv`) that instantiates `axil_shell` and `counter`,
  integration testbench (`top/dv/`) that checks the AXI path and counter
  wiring through the hierarchy, and synthesis scaffolding under `top/syn/`
  (vendor-agnostic stubs).
- `top/plover.core` — FuseSoC core depending on the unit cores, so a
  single resolve pulls in the full design.
- **`syscon` integrated into the project top.** `plover.sv` gains a second
  AXI-Lite slave port (`s_syscon_*`) routed to a `syscon` instance, and
  `syscon.soft_rst_n` gates the `counter`'s reset so a software write to
  `SOFT_RST.CORE` actually resets the counter while the AXI endpoints
  stay alive. Top harness adds include-dir handling for syscon's
  generated version header and passes deterministic `VERSION_OVERRIDE` /
  `VERSION_HASH_OVERRIDE` parameters. The integration `smoke` test now
  also reads syscon's VERSION via the second slave and exercises the
  soft-reset → counter-cleared path; bug injection on the soft-reset
  wiring fails it loudly.
- README section documenting the project-wide **32-bit register-width
  decision** and a roadmap for migrating selected registers (or the whole
  interface) to 64-bit if a future need arises.
- **`top/host/` — host-side C "firmware" subtree.** Plain C ABI
  (`plover_host_ops` callback struct + `plover_hello_world` entry point)
  built as `libplover_hello.so` via `top/host/Makefile`. The Python side
  (`top/dv/firmware_bridge.py`) loads the .so via ctypes and wraps the
  cocotb `AxiLiteMaster` instances in callbacks using cocotb 2.x's
  `cocotb.task.bridge` / `cocotb.task.resume`, so C register accesses
  block the bridge thread while the cocotb event loop services one bus
  transaction at a time. A new pyuvm test (`firmware_smoke` in
  `test_plover.py`) calls into the C from cocotb, proving the round-trip
  (C → Python → cocotb → Verilator → RTL → back) works end-to-end. Bug
  injection on the C side makes the test fail loudly, so it has real
  teeth. Build artifacts (`*.o`, `*.so`) are git-ignored and rebuilt
  on-demand by the pytest harness when sources are newer.
- **Auto-generated C register-access layer from SystemRDL.** The
  firmware reads registers via `offsetof()` on a packed struct generated
  by `peakrdl-cheader` (one per RDL unit, e.g. `axil_shell_t`,
  `syscon_t`), with bitfields extracted via the generator's `_bm` / `_bp`
  macros. Two small helper macros in `top/host/plover_hello.c`
  (`REG_OFFSET`, `FIELD_GET`) keep call sites readable. A
  `_Static_assert` against the generator-emitted `_reset` value catches
  drift between firmware constants and the RDL at compile time. The
  pytest harness threads the FuseSoC-build generated-header paths
  through the `PLOVER_RDL_INCLUDE_DIRS` env var into the firmware
  compile, and the harness's EDAM scanner separates HDL include dirs
  (for Verilator) from C include dirs (for the firmware build).
- **`units/stream_sink/` — AXI4-Stream sink (verification stub).** Pure
  RTL block, no RDL, no software interface. AXI4-Stream slave with
  TDATA[31:0]/TVALID/TREADY/TLAST, always-asserted TREADY (no
  backpressure), counts accepted beats into `beat_count` and XORs TDATA
  into `data_xor`. Standalone DV uses cocotbext-axi's `AxiStreamSource`
  to drive a known pattern and verify both outputs.
- **stream_sink integrated into the project top.** `plover.sv` gains
  an `s_axis_*` input port (TDATA/TVALID/TREADY/TLAST) wired straight
  to the new sink, and two new debug output ports `sink_beat_count` /
  `sink_data_xor` carrying the sink's running state.
- **`firmware_concurrent` integration test.** A new pyuvm test
  (`test_plover.py`) runs cocotb's AXI-Stream stimulus into the
  `stream_sink` in a `cocotb.start_soon` background coroutine while the
  C firmware does its register-access work on the AXI-Lite slaves.
  After both finish, the test asserts the C ran to success AND the
  sink received the expected 16-beat pattern with the expected XOR.
  Includes a probe assertion that at least one beat landed *during*
  the firmware execution (not strictly serialized after it); if AXIS
  ever ends up running entirely after the C, that assertion catches
  the regression. Standalone characterization shows 3 beats land within
  the first 5 cycles, so genuine bus-level parallelism is happening.

### Changed
- **Layout reorganized to colocate RTL and DV per unit** under
  `units/<unit>/{rtl,rdl,dv,*.core}`. Each block is now self-contained;
  adding a new unit is one directory copy. The top-level `rtl/` and `dv/`
  directories no longer exist.
- Per-unit `dv/` directories are now flat (no inner Python package); cocotb
  `test_module` references and intra-package imports updated accordingly.
- `conftest.py` moved to repo root; pytest `testpaths` widened to
  `[units, top]`.
- RDL generator writes regmap to `units/axil_shell/dv/regmap.py` (flat).
- README layout tree and "Adding a new sub-unit" section rewritten;
  new "Project top" section added.
- Makefile `clean` updated for the new layout.

### Notes
- The integration in `top/rtl/plover.sv` is structural-only at this
  stage: `axil_shell` does not yet expose its `CONTROL` register bits as
  ports, so the counter's `enable`/`clear` are tied to constants. Comments
  in both the RTL and the integration testbench flag this as a follow-up.

## [0.1.0] - 2026-05-26

Initial release. An AXI4-Lite top-level shell with a simple register endpoint,
verified with cocotb + Verilator + pyuvm on the `pyuvm-dv-lib` base classes,
built with FuseSoC and run under pytest, with a SystemRDL register map as the
single source of truth.

### RTL
- `rtl/axil_shell.sv` — hand-written AXI4-Lite register endpoint DUT
  (SCRATCH / CONTROL / STATUS / ID).

### Verification
- `dv/axil_shell/` — UVM testbench on the `DVBase*` classes: AXI-Lite agent
  (item, cfg, `cocotbext-axi` BFM driver, monitor), env with a scoreboard whose
  golden model is driven by the generated register map, a virtual sequencer,
  smoke + sweep virtual sequences, and `AxilBaseTest` (which owns the run-phase
  objection per the pyuvm idiom, leaving `dv_lib` untouched).
- `dv/test_axil_shell_pytest.py` — pytest harness that resolves sources through
  FuseSoC's EDAM manifest and runs cocotb/pyuvm via the runner API. Waveform
  dumping is opt-in via `--waves` / `WAVES=1` (writes `dv/dump.vcd`).

### Register map
- `rdl/axil_shell.rdl` — SystemRDL description, the single source of truth.
- `rdl/gen_regs.py` + `rdl/rdl_gen.py` — generate a dependency-free Python
  regmap (consumed by the TB), HTML docs, and a C header; wired into
  `axil_shell.core` as a FuseSoC generator so they regenerate at build.

### Build / tooling
- `axil_shell.core` — FuseSoC CAPI2 core (default + lint targets, RDL generator).
- Dependencies managed with [uv](https://docs.astral.sh/uv/): `pyproject.toml`
  declares them (with `dv_lib` as a git source), `uv.lock` pins exact versions,
  and `.python-version` pins the interpreter. Run via `uv run pytest`.
- `Makefile` — convenience wrappers (`sync`, `test`, `waves`, `lint`, `regs`,
  `clean`).
- `pyproject.toml` — also holds pytest config so bare `pytest` works from the
  root and skips the cocotb-only modules.
- `.github/workflows/ci.yml` — CI (using `uv sync --frozen`) running lint + the
  testbench on push/PR.
- `.editorconfig`, Apache-2.0 `LICENSE` (matching upstream `pyuvm-dv-lib`).

### Notes
- Verilator is provided by the `verilator` wheel (recent release), pinned in
  `uv.lock`, avoiding the VPI-API mismatch between cocotb 2.x and older system
  Verilator packages.
