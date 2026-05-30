# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and the project aims
to follow semantic versioning.

## [Unreleased]

### Added
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
- **`top/host/` — host-side C++ "firmware" subtree.** Plain C ABI
  (`plover_host_ops` callback struct + `plover_hello_world` entry point)
  built as `libplover_hello.so` via `top/host/Makefile`. The Python side
  (`top/dv/firmware_bridge.py`) loads the .so via ctypes and wraps the
  cocotb `AxiLiteMaster` instances in callbacks using cocotb 2.x's
  `cocotb.task.bridge` / `cocotb.task.resume`, so C++ register accesses
  block the bridge thread while the cocotb event loop services one bus
  transaction at a time. A new pyuvm test (`firmware_smoke` in
  `test_plover.py`) calls into the C++ from cocotb, proving the round-trip
  (C++ → Python → cocotb → Verilator → RTL → back) works end-to-end. Bug
  injection on the C++ side makes the test fail loudly, so it has real
  teeth. Build artifacts (`*.o`, `*.so`) are git-ignored and rebuilt
  on-demand by the pytest harness when sources are newer.
- **Auto-generated C++ register-access layer from SystemRDL.** Added
  `peakrdl-cpp>=0.3` to the dependencies and extended `tools/gen_regs.py`
  and `tools/rdl_gen.py` to emit a typed C++ header (`<unit>_regs.hh`)
  alongside the existing C header. Each generated header lives in its own
  namespace (`axil_shell_regs::axil_shell<BusT>`, `syscon_regs::syscon<BusT>`)
  because peakrdl-cpp emits common scope-level helpers; namespacing per
  unit lets them coexist in one translation unit. The firmware
  (`top/host/plover_hello.cc`) was rewritten from raw `host_ops` calls
  to typed accessors: `shell.ID.VALUE.read()`, `syscon.VERSION.read()`.
  A small `BusAdapter` class wraps the existing `plover_host_ops`
  callback pair to satisfy peakrdl-cpp's bus concept, so the C++ runtime
  path is unchanged — only the source-level expression of register access
  is different. The pytest harness threads the FuseSoC-build generated-
  header paths through the `PLOVER_RDL_INCLUDE_DIRS` env var into the
  firmware compile, and the harness's EDAM scanner was extended to
  separate HDL include dirs (for Verilator) from C/C++ include dirs (for
  the firmware build).
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
  C++ firmware does its register-access work on the AXI-Lite slaves.
  After both finish, the test asserts the C++ ran to success AND the
  sink received the expected 16-beat pattern with the expected XOR.
  Includes a probe assertion that at least one beat landed *during*
  the firmware execution (not strictly serialized after it); if AXIS
  ever ends up running entirely after the C++, that assertion catches
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
