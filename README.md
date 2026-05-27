# plover

**An AXI4-Lite shell + design-verification environment.**

(The project name is just a codename; the design under test is an AXI4-Lite
shell — the HDL module, register map, and FuseSoC core keep the functional
name `axil_shell`.)

A top-level FPGA project shell exposed over a single **AXI4-Lite** slave port,
verified with **cocotb + Verilator + pyuvm**, built on the
[`pyuvm-dv-lib`](https://github.com/tm-treehouse/pyuvm-dv-lib) base classes
(an OpenTitan `dv_lib` port). Sources are managed by **FuseSoC**; tests run
under **pytest**.

The DUT is deliberately minimal — a simple register endpoint — so the whole
verification flow is exercised end to end. Replace `rtl/axil_shell.sv` with
your real design (keeping the `s_axil` port, or adjusting `prefix` in the
agent cfg) and the testbench structure carries over.

## Layout

```
plover/
  axil_shell.core              FuseSoC CAPI2 core: RTL filesets + targets + RDL generator
  fusesoc.conf                 local FuseSoC library config (this dir)
  rdl/
    axil_shell.rdl             SystemRDL register map — single source of truth
    gen_regs.py                generator: RDL -> Python regmap + HTML docs + C header
    rdl_gen.py                 FuseSoC generator wrapper (runs gen_regs at build)
    gen/                       generated docs + C header (git-ignored)
  rtl/
    axil_shell.sv              AXI4-Lite register endpoint (the DUT, hand-written)
  dv/
    conftest.py                pytest config; puts dv/ on sys.path
    test_axil_shell_pytest.py  pytest harness: FuseSoC -> EDAM -> cocotb runner
    axil_shell/
      regmap.py                generated from the RDL (git-ignored)
      axil_agent.py            item, agent cfg, driver (cocotbext-axi BFM), monitor, agent
      axil_env.py              env cfg, scoreboard + RDL-driven reference model, vseqr, env
      axil_test.py             vseqs (smoke, sweep) + AxilBaseTest (owns the objection)
      test_axil_shell.py       cocotb entry: @pyuvm.test() classes, cfg wiring
  pyproject.toml             project metadata + dependencies (uv) + pytest config
  uv.lock                    pinned dependency versions (committed)
  .python-version            pinned Python version for uv
  CHANGELOG.md
  LICENSE
```

## Register map

| Offset | Name    | Access | Notes                          |
|-------:|---------|--------|--------------------------------|
| `0x00` | SCRATCH | R/W    | general-purpose scratch        |
| `0x04` | CONTROL | R/W    | control bits (design-defined)  |
| `0x08` | STATUS  | RO     | bit0 mirrors CONTROL[0]        |
| `0x0C` | ID      | RO     | constant `0xC0C07B01`          |

The map is described once in **`rdl/axil_shell.rdl`** (SystemRDL) and is the
single source of truth. A FuseSoC generator runs `rdl/gen_regs.py` at build
time to produce, from that one file:

* `dv/axil_shell/regmap.py` — a small, dependency-free Python register map
  (offsets, field positions, masks, reset values, access) that the testbench
  imports. Sequences address registers by name (`rm.SCRATCH.offset`) and the
  scoreboard's golden model derives its addresses/masks/resets from it, so
  editing the RDL and rebuilding updates the testbench with no hand-changes.
* `rdl/gen/html/` — browsable HTML register documentation (PeakRDL-html).
* `rdl/gen/axil_shell_regs.h` — a C header for software (PeakRDL-cheader).

Because the same `regmap.py` has no PeakRDL runtime dependency, it can be
reused elsewhere — software models, other blocks, or a larger system that
instantiates this shell. The hand-written `rtl/axil_shell.sv` is kept as-is;
the RDL describes and documents its map rather than generating it. To
regenerate manually (the build does it automatically):

```bash
python rdl/gen_regs.py            # regmap + docs + C header
python rdl/gen_regs.py --no-docs  # just the Python regmap
```

## Quick start

Dependencies are managed with [uv](https://docs.astral.sh/uv/). `uv` reads
`pyproject.toml` + `uv.lock`, creates an isolated `.venv`, and runs commands in
it — including the `dv_lib` base library (pulled from GitHub) and the Verilator
wheel, so there's nothing to install by hand.

```bash
uv sync                   # create the environment from the lockfile
uv run pytest             # build with Verilator + run both pyuvm tests
uv run pytest -k smoke    # just the smoke test
uv run pytest -k sweep    # just the randomized sweep
uv run pytest --sim=icarus  # run on Icarus Verilog instead
```

(`uv run` re-checks the lockfile/venv before each command, so a bare
`uv run pytest` is enough — the explicit `uv sync` above is just to show the
step.)

FuseSoC can also lint the RTL directly:

```bash
uv run fusesoc run --target=lint axil_shell
```

For convenience, a `Makefile` wraps the common commands (it's optional — the
real flow is `uv run pytest` / `uv run fusesoc`):

```bash
make test     # run the testbench (pytest)
make lint     # Verilator lint via FuseSoC
make regs     # regenerate the register map + docs from the RDL
make clean    # remove build / sim / generated artifacts
make help     # list targets
```

CI runs lint + the full testbench on every push and pull request
(`.github/workflows/ci.yml`).

## Waveforms

Runs are trace-free by default (faster). To dump waveforms, enable them with
the `--waves` flag, the `WAVES=1` env var, or the make target:

```bash
uv run pytest -k smoke --waves   # dump waves for the smoke test
WAVES=1 uv run pytest            # or via env var, for all tests
make waves                       # convenience wrapper
```

The dump is written to `dv/dump.vcd` (the RTL is compiled with
`--trace-structs`, so the AXI-Lite interface, clock, and reset are all
captured). Open it in a viewer:

```bash
gtkwave dv/dump.vcd     # or: surfer dv/dump.vcd
```

Dumps are git-ignored. For large/long runs, FST is much smaller than VCD —
add `--trace-fst` to the Verilator build args in `dv/test_axil_shell_pytest.py`
(`BUILD_ARGS["verilator"]`) and the dump becomes `dv/dump.fst`.

## How the flow fits together

* **FuseSoC** is the IP/source metadata layer. `axil_shell.core` declares the
  RTL filesets, the toplevel, and parameters. The pytest harness runs
  `fusesoc run --setup` to resolve the design into an **EDAM** manifest and
  reads the source list + toplevel from it — so there is one source of truth
  for "what RTL is in this design," and the same core can later be synthesized
  or depended on by a larger system.
* **pytest** is the orchestration layer. For each test it builds the resolved
  RTL with cocotb's runner API (`get_runner("verilator")`) and runs one
  `@pyuvm.test()` test. `pytest -k` selects tests; failures are per-test.
* **pyuvm + dv_lib** provide the UVM structure: the agent/driver/monitor,
  env/scoreboard/virtual-sequencer, and the test/vseq hierarchy all extend the
  `DVBase*` classes. The driver wraps Alex Forencich's `cocotbext-axi`
  `AxiLiteMaster` BFM; the scoreboard checks every read against an independent
  Python reference model of the register map.

## Two design notes worth knowing

**The run-phase objection lives in `AxilBaseTest`, not the library.**
`dv_lib.DVBaseTest` is a faithful port of SystemVerilog `dv_base_test`, where
phase objections are implicit. pyuvm's phaser is not implicit — its
`ObjectionHandler` keeps the run phase alive only while some component holds an
objection — so the standard pyuvm idiom is for the *test's* `run_phase` to
`raise_objection()` while the sequence runs and `drop_objection()` after.
`AxilBaseTest.run_phase` does exactly that, bracketing `super().run_phase()`.
The dv_lib base is left untouched; the `@pyuvm.test()` entry classes only wire
the cfg. (If you'd prefer every dv_lib testbench to get this for free, the
objection could instead be added once in `DVBaseTest.run_phase` upstream.)

**Verilator comes from the `verilator` wheel.** cocotb 2.x calls VPI
functions (`clearEvalNeeded`, `doInertialPuts`) that older system Verilator
packages don't provide, which breaks the build. The `verilator==5.48.0` wheel
(pinned in `uv.lock`, installed into the project's `.venv`) ships a recent
release binary; the harness points `VERILATOR_ROOT`/`PATH` at it automatically
so cocotb uses it rather than any older system install.
