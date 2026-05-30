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
  dv/                                  shared DV components (project-local, imported as `dv`)
    axi_lite_agent.py                  AxiLiteAgent (item/cfg/driver/monitor) on dv_lib bases
    axi_stream_agent.py                AxiStreamAgent (stimulus side), same shape
  tools/                               build-time scripts (run by FuseSoC + harnesses)
    dv_harness.py                      shared FuseSoC + cocotb runner — every unit's pytest shim imports this
    gen_regs.py                        RDL -> Python regmap + HTML docs + C header (shared across RDL units)
    rdl_gen.py                         FuseSoC generator wrapper (runs gen_regs at build)
    version_gen.py                     syscon version-header generator
  units/                               sub-blocks, each self-contained
    axil_shell/                        AXI4-Lite register endpoint (RDL-driven)
      axil_shell.core
      rtl/axil_shell.sv
      rdl/axil_shell.rdl               SystemRDL — single source of truth for this block's regs
      dv/                              regmap.py (generated, ignored) + env + test + cocotb entry + shim
    counter/                           template sub-unit (no RDL)
      counter.core
      rtl/counter.sv
      dv/<env + test + cocotb entry + shim>
    syscon/                            system controller (RDL + git-version generator)
      syscon.core
      rtl/syscon.sv
      rdl/syscon.rdl
      rdl/gen_version.py
      dv/<...same shape as axil_shell/dv/...>
    stream_sink/                       AXI-Stream sink (verification stub)
      stream_sink.core
      rtl/stream_sink.sv
      dv/<env + test + cocotb entry + shim>
    axil_xbar/                         AXI-Lite 1-to-N decoder with register stages
      axil_xbar.core
      rtl/axil_xbar.sv                 the decoder
      rtl/axil_skid_buffer.sv          register-slice helper used inside
      dv/axil_xbar_dv_top.sv           DV harness (xbar + 2 RAM stubs)
      dv/axil_ram_stub.sv              behavioural AXI-Lite RAM (DV only)
      dv/<env + test + cocotb entry + shim>
  top/                              project top — integrates the units above
    plover.core                        FuseSoC core (depends on all five unit cores)
    rtl/plover.sv                      top-level integration (single AXI-Lite slave via xbar)
    dv/
      plover_env.py                    two-agent env (AxiLite + AxiStream) + minimal scoreboard
      plover_test.py                   three vseqs + base test; firmware bridge composed here
      test_plover.py                   cocotb entry: smoke, firmware_smoke, firmware_concurrent
      test_plover_pytest.py            pytest shim over tools/dv_harness.py
      firmware_bridge.py               ctypes loader + cocotb bridge for host/
    host/                              host-side C "firmware" that drives plover via AXI
      plover_hello.h
      plover_hello.c
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

Each block lives under `units/<unit>/`:

```
units/foo/
  foo.core                             FuseSoC core (rtl + lint targets)
  rtl/foo.sv                           the RTL
  dv/
    foo_env.py                         env cfg, scoreboard + ref model, vseqr, env class
    foo_test.py                        vseqs + FooBaseTest (owns the run-phase objection)
    test_foo.py                        cocotb entry — @pyuvm.test() classes + cfg wiring
    test_foo_pytest.py                 pytest shim over tools/dv_harness.py (~30 lines)
```

The DV builds on the [pyuvm-dv-lib](https://github.com/tm-treehouse/pyuvm-dv-lib)
base classes (port of OpenTitan's `dv_lib`) for the env/agent/scoreboard
scaffolding, and on project-local protocol agents from `dv/` for the
actual AXI-Lite or AXI-Stream stimulus side. Don't write a new
`*_agent.py` per unit — import `AxiLiteAgent` / `AxiStreamAgent` from
`dv` and configure them with the unit's signal `prefix`.

The pytest shim is mechanical:

```python
import sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]
sys.path.insert(0, str(ROOT))

from tools.dv_harness import HarnessConfig, run_testcase

CFG = HarnessConfig(core_name="foo", test_module="test_foo", here=HERE, root=ROOT)
TESTCASES = ["smoke"]


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_foo(cocotb_testcase):
    run_testcase(CFG, cocotb_testcase)
```

That's it — FuseSoC scans the whole tree for `.core` files, pytest
matches `test_*_pytest.py`, so the new unit is picked up automatically.
Optional `HarnessConfig` knobs cover build parameters (`parameters=`),
extra DV-only SV sources (`extra_sources=`), C/C++ include passthrough
(`c_include_env=`), and project-top-style live-dir overrides
(`live_dir_map=`); see `tools/dv_harness.py` for the full list.

For an RDL-bearing unit, add `rdl/foo.rdl` and a `regmap` generator
block in the .core (see `units/axil_shell/axil_shell.core` for the
pattern). `tools/gen_regs.py` and `tools/rdl_gen.py` do the work; the
`.rdl` is the single source of truth and the generated artifacts
(`regmap.py`, HTML docs, C header) regenerate at every build.

## Project top (`top/`)

The `top/` directory sits parallel to `units/` and holds the project's
top-level integration — the RTL that instantiates the verified sub-units,
its own integration testbench, and synthesis scaffolding.

```
top/
  plover.core      FuseSoC core (depends on ::axil_shell, ::counter, ::syscon, ::stream_sink, ::axil_xbar)
  rtl/plover.sv    structural top: instantiates all sub-units behind one AXI-Lite port
  dv/              integration testbench (cocotb + pyuvm)
  host/            host-side C firmware (see next section)
  syn/             synthesis scaffolding (vendor-agnostic — see syn/README.md)
```

The top exposes two external bus interfaces:

* `s_axil_*` — AXI4-Lite slave (32-bit address, 32-bit data). The single
  host-facing register bus. The xbar inside the top routes by address:
  | Range                              | Slave        |
  | ---------------------------------- | ------------ |
  | `0x0000_0000` .. `0x0000_0FFF`     | `axil_shell` |
  | `0x0000_1000` .. `0x0000_1FFF`     | `syscon`     |
  | anything else                      | DECERR       |

  4 KB pages give each peripheral room to grow without remap. The xbar is
  a unit at `units/axil_xbar/` with its own DV (see below).

* `s_axis_*` — AXI4-Stream slave routed to `stream_sink` (TDATA[31:0],
  TVALID, TREADY, TLAST). The sink absorbs every beat without
  backpressure, counts beats into `sink_beat_count`, and folds TDATA
  into a running XOR `sink_data_xor` — both exposed as top-level debug
  outputs.

`syscon`'s `soft_rst_n` is ANDed with the global `rst_n` to form the
`counter`'s reset, so a software write to `SOFT_RST.CORE` (at host-visible
address `0x0000_1008`) holds the counter in reset for the syscon pulse
width while the AXI endpoints stay alive.

The integration testbench has narrow scope by design: it does not re-verify
the sub-units (they have their own DV under `units/`) — it only checks that
the integration is **wired and alive**. Three pyuvm tests live here:

`smoke`
: Reads `axil_shell.ID` at `0x0000_000C` and `syscon.VERSION` at
  `0x0000_1000` via the single AXI master. Writes/reads an unmapped
  address (`0x0000_2000`) and confirms the xbar returns DECERR.
  Samples `dut.count` across cycles. Writes `1` to `syscon.SOFT_RST.CORE`,
  verifies the counter is held at 0 mid-window and has restarted from
  0 after the soft-reset pulse ends.

`firmware_smoke`
: Same setup, but the *test logic* runs in C from `top/host/` via the
  ctypes bridge. The bridge exposes one read/write pair (matching the
  unified bus topology) and the firmware computes absolute addresses
  from host-supplied page bases. See the "Host-side C" section below.

`firmware_concurrent`
: cocotb pushes AXI-Stream stimulus into `stream_sink` in a background
  coroutine while the C firmware does its register work. Both stimuli
  sources are live simultaneously on independent buses; the test asserts
  the C ran to success AND the sink received the expected pattern, with
  an extra probe assertion that beats landed *during* the firmware run.

Bug injection on any of the wired-up paths (swapping the xbar page bases,
tying `counter_enable=0`, bypassing `soft_rst_n`, truncating the AXIS
stimulus, mis-stating an expected register value in the C) makes the
relevant check fail loudly.

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

## Host-side C (`top/host/`)

`top/host/` holds C "firmware" — host-side code that drives the chip
through register reads and writes. The same source could later
cross-compile and run on a real CPU mastering AXI on a board; only the
read/write implementations change. Today it runs against the cocotb
testbench, using cocotb 2.x's `cocotb.task.bridge` / `cocotb.task.resume`
to wire the C's synchronous register accesses into the simulator's
async event loop.

```
top/host/
  plover_hello.h    C ABI: plover_host_ops callback struct + entry points
  plover_hello.c    firmware test routines (uses generated peakrdl-cheader output)
  Makefile          builds libplover_hello.so with gcc -std=c11
```

The plumbing on the Python side lives in `top/dv/firmware_bridge.py`. It
builds the `.so` on demand, loads it via ctypes, and exposes
`run_hello_world(host, *, shell_base, syscon_base, expected_syscon_version, include_dirs)`
as an awaitable that runs the C test routine. `include_dirs` carries the
FuseSoC build paths where peakrdl-cheader dropped the generated register
headers (`axil_shell_regs.h`, `syscon_regs.h`) so the firmware compile
picks them up.

### Single-bus model

After the project-top xbar refactor, the host sees one AXI-Lite master at
the top of the chip and each peripheral lives at a page base on that
unified bus. The `plover_host_ops` struct therefore carries a single
`read`/`write` callback pair (not per-peripheral pairs), and
`plover_hello_world` takes the peripherals' page bases as arguments so
firmware can compute absolute addresses:

```c
int plover_hello_world(const plover_host_ops* ops,
                       uint32_t shell_base,
                       uint32_t syscon_base,
                       uint32_t expected_syscon_version);
```

This mirrors how real software would see the chip: one MMIO window, with
each peripheral mapped at a known base.

### Register access via auto-generated C headers

The firmware does **not** hardcode register addresses. Each unit's
`.rdl` is fed through `peakrdl-cheader` (via the same FuseSoC generator
that emits the Python regmap + HTML docs), producing a header with two
useful artifacts per unit:

* A `__attribute__((__packed__))` struct mirroring the register layout
  (`axil_shell_t`, `syscon_t`). The struct's field offsets give byte
  addresses; a generator-emitted `_Static_assert` confirms the total
  size matches the address map.
* Per-field `_bm` (bitmask), `_bp` (bit position), `_bw` (width), and
  `_reset` macros — e.g. `AXIL_SHELL__ID__VALUE_bm` and
  `AXIL_SHELL__ID__VALUE_bp` for the VALUE field of the ID register.

The firmware uses three small helper macros to keep call sites readable:

```c
#define REG_OFFSET(type, field)     ((uint32_t)offsetof(type, field))
#define REG_ADDR(base, type, field) ((base) + REG_OFFSET(type, field))
#define FIELD_GET(raw, prefix)      (((raw) & prefix##_bm) >> prefix##_bp)

uint32_t id_addr  = REG_ADDR(shell_base, axil_shell_t, ID);
uint32_t id_raw   = ops->read(id_addr);
uint32_t id_value = FIELD_GET(id_raw, AXIL_SHELL__ID__VALUE);
```

A typo in the field name (`offsetof(axil_shell_t, ID_TYPO)`) fails at
compile time. A separate `_Static_assert` in the firmware pins
`EXPECTED_SHELL_ID` against `AXIL_SHELL__ID__VALUE_reset`, so any RDL
change that moves the ID value also fails the build, not just the test.

### Two tests live on this stack

`firmware_smoke`
: The minimum useful check: read the shell ID and the syscon VERSION
  through the single host_ops callbacks and confirm both match. Proves
  the chain C → Python → cocotb → Verilator → RTL → back works end-to-end.
  Bug-injection on either expected value fails the test loudly.

`firmware_concurrent`
: cocotb's `AxiStreamSource` pushes a 16-beat pattern into the
  `stream_sink` unit via `s_axis_*` on a background coroutine
  (`cocotb.start_soon`) while the C firmware reads its registers on
  the AXI-Lite path. After both finish, the test asserts the C ran
  to success AND the sink's `beat_count` matches the expected pattern
  length with the expected XOR — proving the two stimuli sources were
  live at the same simulated time on independent buses. The test
  includes a probe assertion that some beats land *during* the
  firmware run; if AXIS was somehow being serialized after the C
  rather than running in parallel, that assertion catches it.

### Useful properties

* **No new build system at the top level.** The shared object is rebuilt
  on every run by the pytest harness via `top/host/Makefile`. Adding new
  firmware files is editing the Makefile's `SRCS` list; pytest picks up
  the new code automatically.

* **Plain portable C11.** Nothing platform-specific in the source. The
  C ABI surface (`plover_host_ops`, function pointers) makes this
  ctypes-loadable without a binding-generator layer, and the small
  toolchain footprint (just gcc, no C++ runtime) makes cross-compilation
  to embedded targets easy when that becomes useful.

* **Real firmware-style code.** The C has no idea what an AXI handshake
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
