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
plover/                                project root
  fusesoc.conf                         local FuseSoC library config
  conftest.py                          repo-level pytest options (--sim / --waves)
  pyproject.toml                       project metadata + dependencies (uv) + pytest config
  uv.lock                              pinned dependency versions (committed)
  .python-version                      pinned Python version for uv
  units/                               sub-blocks, each self-contained
    axil_shell/
      axil_shell.core                  FuseSoC core: RTL filesets + targets + RDL generator
      rtl/axil_shell.sv                AXI4-Lite register endpoint (the DUT)
      rdl/axil_shell.rdl               SystemRDL register map — single source of truth
      rdl/gen_regs.py                  RDL -> Python regmap + HTML docs + C header
      rdl/rdl_gen.py                   FuseSoC generator wrapper (runs gen_regs at build)
      rdl/gen/                         generated docs + C header (git-ignored)
      dv/
        regmap.py                      generated from the RDL (git-ignored)
        axil_agent.py                  item, agent cfg, driver (cocotbext-axi BFM), monitor, agent
        axil_env.py                    env cfg, scoreboard + RDL-driven reference model, vseqr, env
        axil_test.py                   vseqs (smoke, sweep) + AxilBaseTest (owns the objection)
        test_axil_shell.py             cocotb entry: @pyuvm.test() classes, cfg wiring
        test_axil_shell_pytest.py      pytest harness: FuseSoC -> EDAM -> cocotb runner
    counter/                           template sub-unit (same shape, no RDL)
      counter.core
      rtl/counter.sv
      dv/<...same shape as axil_shell/dv/...>
    syscon/                            system controller (RDL + version generator)
      syscon.core
      rtl/syscon.sv
      rdl/syscon.rdl
      rdl/gen_version.py
      dv/<...same shape as axil_shell/dv/...>
    stream_sink/                       AXI-Stream sink (verification stub)
      stream_sink.core
      rtl/stream_sink.sv
      dv/<...same shape, no RDL...>
  top/                              project top — integrates the units above
    plover.core                        FuseSoC core (depends on all four unit cores)
    rtl/plover.sv                      top-level integration (structural wiring)
    dv/
      test_plover.py                   cocotb entry: smoke, firmware_smoke, firmware_concurrent
      test_plover_pytest.py            pytest harness for the top
      firmware_bridge.py               ctypes loader + cocotb bridge for host/
    host/                              host-side C++ "firmware" that drives plover via AXI
      plover_hello.h
      plover_hello.cc
      Makefile
    syn/                               synthesis scaffolding (vendor-agnostic stubs)
      constraints/plover.sdc
      scripts/build.sh
      README.md
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

## Register width: 32-bit (and how to migrate to 64-bit)

**Plover uses 32-bit registers over a 32-bit AXI4-Lite interface throughout.**
That's the deliberate default: all `.rdl` files declare
`default regwidth = 32; default accesswidth = 32;`, all unit RTL has
`DATA_WIDTH = 32`, and the testbenches use cocotbext-axi's 32-bit defaults.

### Why 32-bit

* **It matches the protocol.** AXI4-Lite has two legal data widths, 32 and
  64. 32-bit is the near-universal common case — every host CPU, every
  off-the-shelf IP block, and all the cocotbext-axi defaults are built
  around it. Going to 64-bit AXI-Lite cuts the project off from a lot of
  reference IP without much in return for the values we actually carry.
* **Nothing in the current maps needs more than 32 bits.** VERSION,
  VERSION_HASH, SOFT_RST, RESET_CAUSE, FEATURES (syscon) and SCRATCH,
  CONTROL, STATUS, ID (axil_shell) all comfortably fit. Doubling the
  register-file area without a value that needs the width is pure cost.
* **Tooling alignment.** The pyuvm-dv-lib / OpenTitan dv_lib conventions
  this project builds on, the cocotbext-axi BFMs the agents wrap, and the
  PeakRDL exporters all assume 32-bit by default. Staying with the grain
  keeps the project simple.

### When you'd actually want 64-bit

There are real reasons to revisit this, all of which would be triggered by
a specific need rather than a general preference:

* A 64-bit host CPU needs a *single atomic* read/write of a value wider
  than 32 bits (a timestamp, a 64-bit counter, a pointer).
* A specific register has values >32 bits and the LO/HI split would
  introduce real software hazards.
* The project standardizes on a 64-bit interconnect for other reasons
  (DMA, AXI4-full peripherals on the same fabric).

### Roadmap if/when we migrate

Two distinct migration paths, depending on what you actually need:

**Path A — selected 64-bit registers, keep 32-bit AXI-Lite** *(recommended
first step)*. SystemRDL lets you set `regwidth = 64; accesswidth = 32;` on
just the registers that need it; PeakRDL generates LO/HI access logic so
the AXI interface stays 32-bit. This earns its keep only where genuinely
needed and leaves everything else alone.

Concrete steps:
1. In the `.rdl` for the affected register, set `regwidth = 64;
   accesswidth = 32;` on that one register (or on the addrmap, if many).
2. Re-run the RDL generator (`uv run python tools/gen_regs.py`) — the
   Python regmap will pick up the new register widths; the C header gets
   `_LO`/`_HI` accessors.
3. Update the unit's RTL to expose the register as two 32-bit AXI
   offsets. The hand-written `axil_shell.sv`/`syscon.sv` need the case
   statement extended; or generate the regblock from PeakRDL-regblock.
4. Update the Python reference model in the unit's `dv/<unit>_env.py` to
   model the LO/HI access and the implementation-defined update
   semantics (typically: write LO buffers, write HI commits; reads
   snapshot on LO read). Add a vseq that exercises out-of-order access.
5. Re-run `uv sync && uv run pytest` and confirm.

**Path B — full 64-bit AXI-Lite throughout** *(larger, only if needed)*.
Every unit's AXI interface widens to 64-bit data. Steps:
1. RDL: `default regwidth = 64; default accesswidth = 64;` in every
   `.rdl`. Re-run the generator; field positions don't change but
   register widths do.
2. RTL: widen `s_axil_wdata`/`s_axil_rdata` to `[63:0]` and `s_axil_wstrb`
   to `[7:0]`. Adjust internal register storage to 64-bit. Address
   decode shifts from `>>2` to `>>3`.
3. DV: the cocotbext-axi `AxiLiteMaster` is width-inferred from the bus
   signals, so it handles 64-bit automatically. Update the harness's
   `byte_lanes` use and verify the reference models, then re-run.
4. Project top (`top/rtl/plover.sv`) widens the same way; integration
   testbench keeps working as long as the BFM is rebuilt against the new
   bus.

In both paths, the FuseSoC build, generators, and pytest harness need no
changes — they're width-agnostic. The work is concentrated in RDL, RTL,
and reference models, in that order.

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

## Adding a new sub-unit

The `counter` block is a deliberately tiny template for adding more
sub-units. With the colocated layout, each block is self-contained under
`units/<unit>/`:

```
units/counter/
  counter.core                         FuseSoC core (rtl + lint targets)
  rtl/counter.sv                       the RTL
  dv/
    counter_agent.py                   item, cfg, driver, monitor, agent
    counter_env.py                     env cfg, scoreboard + ref model, vseqr, env
    counter_test.py                    vseqs + CounterBaseTest (owns objection)
    test_counter.py                    cocotb entry (@pyuvm.test() classes)
    test_counter_pytest.py             FuseSoC -> EDAM -> cocotb runner
```

To add unit `foo`: copy `units/counter/` to `units/foo/`, rename `counter`
to `foo` throughout (file names, CORE_NAME, TEST_MODULE, class names, `vif`
signal handles), and change the driver / monitor / `RefModel` to match your
block. FuseSoC and pytest pick the new unit up automatically (FuseSoC scans
the whole tree for `.core` files; pytest matches `test_*_pytest.py`).

## Project top (`top/`)

The `top/` directory sits parallel to `units/` and holds the project's
top-level integration — the RTL that instantiates the verified sub-units,
its own integration testbench, and synthesis scaffolding.

```
top/
  plover.core      FuseSoC core (depends on ::axil_shell, ::counter, ::syscon, ::stream_sink)
  rtl/plover.sv    structural top: instantiates all four sub-units
  dv/              integration testbench (cocotb + pyuvm)
  host/            host-side C++ firmware (see next section)
  syn/             synthesis scaffolding (vendor-agnostic — see syn/README.md)
```

The top exposes three external bus interfaces:

* `s_axil_*` — AXI4-Lite slave routed to `axil_shell`
* `s_syscon_*` — AXI4-Lite slave routed to `syscon`
* `s_axis_*` — AXI4-Stream slave routed to `stream_sink` (TDATA[31:0],
  TVALID, TREADY, TLAST). The sink absorbs every beat without
  backpressure, counts beats into `sink_beat_count`, and folds TDATA
  into a running XOR `sink_data_xor` — both exposed as top-level debug
  outputs.

`syscon`'s `soft_rst_n` is ANDed with the global `rst_n` to form the
`counter`'s reset, so a software write to `SOFT_RST.CORE` on the syscon
slave holds the counter in reset for the syscon pulse width while the
AXI endpoints stay alive.

The integration testbench has narrow scope by design: it does not re-verify
the sub-units (they have their own DV under `units/`) — it only checks that
the integration is **wired and alive**. Three pyuvm tests live here:

`smoke`
: Reads `axil_shell`'s ID via `s_axil_*` and `syscon`'s VERSION via
  `s_syscon_*` (both AXI paths). Samples `dut.count` across cycles
  (counter is clocked). Writes `1` to `syscon`'s `SOFT_RST.CORE`,
  verifies the counter is held at 0 mid-window and has restarted from
  0 after the soft-reset pulse ends (soft_rst_n → counter wiring).

`firmware_smoke`
: Same setup, but the *test logic* runs in C++ from `top/host/` via the
  ctypes bridge. See the "Host-side C++" section below.

`firmware_concurrent`
: cocotb pushes AXI-Stream stimulus into `stream_sink` in a background
  coroutine while the C++ firmware does its register work. Both stimuli
  sources are live simultaneously on independent buses; the test asserts
  the C++ ran to success AND the sink received the expected pattern.

Bug injection on any of the wired-up paths (e.g. tying `counter_enable=0`,
bypassing `soft_rst_n`, truncating the AXIS stimulus, mis-stating an
expected register value in the C++) makes the relevant check fail loudly.

**Known limitation, intentionally left as scaffolding**: `axil_shell` does
not currently expose its `CONTROL` register bits as ports, so the counter's
`enable`/`clear` are tied to constants in `plover.sv` rather than driven
from `CONTROL.ENABLE`. Comments in both `top/rtl/plover.sv` and the
integration testbench flag this as a follow-up — extending the shell to
publish CONTROL is the natural next step, after which the integration test
grows a "write CONTROL.ENABLE=0, confirm count freezes" check.

The synthesis scaffolding under `top/syn/` is intentionally
vendor-agnostic at this stage; see `top/syn/README.md` for how to wire a
real synthesis flow into the `syn` target of `plover.core` once a vendor is
picked.

## Host-side C++ (`top/host/`)

`top/host/` holds C++ "firmware" — host-side code that drives the chip
through register reads and writes. The same source could later
cross-compile and run on a real CPU mastering AXI on a board; only the
read/write implementations change. Today it runs against the cocotb
testbench, using cocotb 2.x's `cocotb.task.bridge` / `cocotb.task.resume`
to wire the C++'s synchronous register accesses into the simulator's
async event loop.

```
top/host/
  plover_hello.h    C ABI: plover_host_ops callback struct + entry points
  plover_hello.cc   firmware test routines (uses generated typed accessors)
  Makefile          builds libplover_hello.so
```

The plumbing on the Python side lives in `top/dv/firmware_bridge.py`. It
builds the `.so` on demand, loads it via ctypes, and exposes
`run_hello_world(shell, syscon, expected_version, include_dirs)` as an
awaitable that runs the C++ test routine. `include_dirs` carries the
FuseSoC build paths where peakrdl-cpp dropped the typed register headers
(`axil_shell_regs.hh`, `syscon_regs.hh`) so the firmware compile picks
them up.

### Typed register access via auto-generated C++

The firmware does **not** hardcode register addresses. Each unit's
`.rdl` is also fed through `peakrdl-cpp` (via the same FuseSoC
generator that already emits the Python regmap + C header + HTML docs),
producing a templated C++ class hierarchy per unit. The firmware looks like:

```cpp
// Each generated header owns a per-unit namespace so they can coexist
// in one translation unit (peakrdl-cpp emits common scope-level helpers).
axil_shell_regs::axil_shell<BusAdapter> shell(shell_bus, /*base=*/0);
syscon_regs::syscon<BusAdapter>         syscon(syscon_bus, /*base=*/0);

uint32_t id      = shell.ID.VALUE.read();    // typed field accessor
uint32_t version = syscon.VERSION.read();    // typed register accessor
```

`BusAdapter` is a tiny wrapper around the `plover_host_ops` callback
pair (`read_fn`, `write_fn`) that satisfies peakrdl-cpp's bus concept
(`data_t read(addr_t)`, `void write(addr_t, data_t)`). Because the
generated classes are templated on the bus type, the compiler inlines
straight through to the C function pointers — no virtual dispatch.

### Two tests live on this stack

`firmware_smoke`
: The minimum useful check: read the shell ID and the syscon VERSION
  through the typed accessors and confirm both match. Proves the chain
  C++ → Python → cocotb → Verilator → RTL → back works end-to-end.
  Bug-injection on either expected value fails the test loudly.

`firmware_concurrent`
: cocotb's `AxiStreamSource` pushes a 16-beat pattern into the
  `stream_sink` unit via `s_axis_*` on a background coroutine
  (`cocotb.start_soon`) while the C++ firmware reads its registers on
  the AXI-Lite paths. After both finish, the test asserts the C++ ran
  to success AND the sink's `beat_count` matches the expected pattern
  length with the expected XOR — proving the two stimuli sources were
  live at the same simulated time on independent buses. The test
  includes a probe assertion that some beats land *before* the
  firmware returns; if AXIS was somehow being serialized after the C++
  rather than running in parallel, that assertion catches it.

### Useful properties

* **No new build system at the top level.** The shared object is rebuilt
  on every run by the pytest harness via `top/host/Makefile`. Adding new
  firmware files is editing the Makefile's `SRCS` list; pytest picks up
  the new code automatically.

* **Portable C++17.** Nothing platform-specific in the source. The C ABI
  surface (`plover_host_ops`, function pointers) makes this
  ctypes-loadable without a binding-generator layer.

* **Real firmware-style code.** The C++ has no idea what an AXI handshake
  looks like; it sees register reads and writes. The same routine could
  run on a real chip with `mmap`'d MMIO regions instead of `host_ops`
  callbacks.

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
