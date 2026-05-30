# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims
to follow semantic versioning.

## [Unreleased]

### Added
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
