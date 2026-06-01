# `tools/` — build-time scripts

Scripts that run at FuseSoC build time or as pytest fixtures, not at
simulation time. The directory holds four entries, each independent:

| File              | What it does                                     |
| ----------------- | ------------------------------------------------ |
| `dv_harness.py`   | Shared FuseSoC + cocotb test runner.             |
| `rdl_gen.py`      | FuseSoC generator wrapper that invokes gen_regs. |
| `gen_regs.py`     | SystemRDL -> Python regmap + HTML + C header.    |
| `version_gen.py`  | Git-version header generator (syscon only).      |

## `dv_harness.py` — the shared pytest harness

Every unit's pytest shim (`test_<unit>_pytest.py`) imports
`HarnessConfig` and `run_testcase` from here. The harness:

1. Builds the FuseSoC core via Edalize (Verilator backend).
2. Locates the generated Verilog runtime.
3. Spawns `cocotb-runner` with the test module + testcase name.
4. Parses the resulting JUnit XML and reports pass/fail to pytest.

`HarnessConfig` is a dataclass with the per-unit fields (core_name,
test_module, parameters, env vars, ...). `run_testcase(cfg, name)`
runs one pyuvm-registered testcase from `cfg.test_module`.

The harness exists because every unit's pytest shim was the same 100
lines of boilerplate around build + run. Concentrating it here means a
new unit's shim is ~30 lines.

## `rdl_gen.py` and `gen_regs.py` — RDL pipeline

Two-step pipeline: `gen_regs.py` does the work; `rdl_gen.py` is the
FuseSoC `generator` entrypoint that calls it.

`gen_regs.py` reads a SystemRDL file and emits three artifacts:

* `regmap.py` — dependency-free Python module describing the
  register map. Imported by the unit's pyuvm env to drive sequences
  and predict responses.
* `<unit>_regs.h` — C header with offsets and field masks. Used by
  `top/host/` firmware.
* `<unit>_regs.html` — human-readable documentation rendered from
  the RDL.

The `regmap.py` files are git-ignored — they're regenerated at build
time. The C header and HTML are emitted to the FuseSoC build directory
alongside other generated artifacts.

`rdl_gen.py` is the FuseSoC generator manifest's entry point. It
receives the RDL path and output directory from FuseSoC's generator
infrastructure and forwards to `gen_regs.run()`.

Units that use RDL today:

* `axil_shell` — has the canonical RDL pipeline wiring; copy from
  here when adding a new RDL-driven unit.
* `syscon` — uses RDL for register definitions and additionally
  calls `version_gen.py` (next section).

## `version_gen.py` — git-version header

Generates `syscon_version_pkg.svh` from the project's git state. Used
only by `syscon` (which embeds the version in a read-only register).

Run at FuseSoC build time via syscon's own generator manifest. Emits a
SystemVerilog package with two parameters: a packed `[31:0]` version
constant and a packed `[31:0]` hash. The format is loosely
"semver-with-dirty-flag": major/minor/patch in the version, top byte
of the hash uses bit 31 to flag dirty trees.

The integration testbench passes deterministic values via
`VERSION_OVERRIDE`/`VERSION_HASH_OVERRIDE` parameters on `syscon` so
test expectations don't depend on the working tree's git state.
