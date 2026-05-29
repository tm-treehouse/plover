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
- README section documenting the project-wide **32-bit register-width
  decision** and a roadmap for migrating selected registers (or the whole
  interface) to 64-bit if a future need arises.

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
