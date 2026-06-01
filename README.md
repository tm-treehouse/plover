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
    axi_stream_agent.py                AxiStreamAgent — role-aware (source / sink)
    dsp_models.py                      bit-exact Python references (CIC, FIR, CicFirChain)
    dsp_plot.py                        comparison-plot helper for DSP tests
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
    stream_sink/                       AXI-Stream sink (verification stub, unit only)
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
    cic_decimator/                     CIC decimation filter (Pattern A DV)
      cic_decimator.core
      rtl/cic_decimator.sv
      dv/<test + cocotb entry + shim>
    cic_interpolator/                  CIC interpolation filter (Pattern A DV)
      cic_interpolator.core
      rtl/cic_interpolator.sv
      dv/<test + cocotb entry + shim>
    fir_filter/                        Direct-form FIR with AXI-Lite coefficient bank
      fir_filter.core
      rtl/fir_filter.sv
      dv/<test + cocotb entry + shim>
  top/                                 project top — integrates the units above
    plover.core                        FuseSoC core (depends on all unit cores it integrates)
    rtl/plover.sv                      AXI-Lite via xbar + CIC -> FIR signal chain
    dv/
      plover_env.py                    three-agent env (AxiLite + AxiStream-in + AxiStream-out) + DSP-aware scoreboard
      plover_test.py                   five vseqs + base test; firmware bridge composed here
      test_plover.py                   cocotb entry: smoke + firmware_* + chain_*
      test_plover_pytest.py            pytest shim over tools/dv_harness.py
      firmware_bridge.py               ctypes loader + cocotb bridge for host/
    host/                              host-side C "firmware" that drives plover via AXI
      plover_hello.h                   API: plover_hello_world + plover_program_fir
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
  plover.core      FuseSoC core (depends on axil_shell + counter + syscon + axil_xbar + cic_decimator + fir_filter)
  rtl/plover.sv    structural top: AXI-Lite via xbar + DSP signal chain (CIC -> FIR)
  dv/              integration testbench (cocotb + pyuvm)
  host/            host-side C firmware (see next section)
  syn/             synthesis scaffolding (vendor-agnostic — see syn/README.md)
```

The top exposes two external bus interfaces and one inline DSP path:

* `s_axil_*` — AXI4-Lite slave (32-bit address, 32-bit data). The single
  host-facing register bus. The xbar inside the top routes by address:
  | Range                              | Slave         |
  | ---------------------------------- | ------------- |
  | `0x0000_0000` .. `0x0000_0FFF`     | `axil_shell`  |
  | `0x0000_1000` .. `0x0000_1FFF`     | `syscon`      |
  | `0x0000_2000` .. `0x0000_2FFF`     | `fir_filter`  |
  | anything else                      | DECERR        |

  4 KB pages give each peripheral room to grow without remap. The xbar is
  a unit at `units/axil_xbar/` with its own DV (see below). The FIR's
  page hosts its coefficient bank: byte offset `4*i` selects coefficient
  `i`, so software writes a tap table by walking word-aligned addresses
  from `0x0000_2000`.

* `s_axis_*` — AXI4-Stream slave carrying signed sample data into the
  CIC decimator (TDATA is `SAMPLE_W`-bit signed, default 16). Standard
  TVALID/TREADY handshake.

* `m_axis_*` — AXI4-Stream master carrying filtered sample data out of
  the FIR. Output rate is the input rate divided by `CIC_DECIM` (default
  4); standard handshake with backpressure that propagates upstream.

The inline DSP signal chain inside the top:

```
s_axis_* -> cic_decimator (N=3, R=4, M=1) -> fir_filter (8 taps) -> m_axis_*
```

The FIR's coefficients are hot-updatable from software — writing to the
FIR page changes the filter's behaviour on the next sample. Coefficients
default to zero at reset, so software must program a tap set before
useful samples flow.

`syscon`'s `soft_rst_n` is ANDed with the global `rst_n` to form the
`counter`'s reset, so a software write to `SOFT_RST.CORE` (at
host-visible address `0x0000_1008`) holds the counter in reset for the
syscon pulse width while the AXI endpoints and DSP chain stay alive.

The integration testbench checks both wiring and DSP correctness. The
sub-units (axil_shell, syscon, axil_xbar, cic_decimator, fir_filter)
are unit-verified under `units/`; the top exists to confirm they
compose. Five pyuvm tests live here:

`smoke`
: Reads `axil_shell.ID` and `syscon.VERSION` via the xbar. Writes/reads
  an unmapped address (`0x0000_4000`) and confirms DECERR. Exercises
  `CONTROL.ENABLE`'s gating of the counter (held → advancing → frozen
  → re-enabled), then the syscon soft-reset window.

`firmware_smoke`
: Same setup, but the *test logic* runs in C from `top/host/` via the
  ctypes bridge. The bridge exposes one read/write pair (matching the
  unified bus topology) and the firmware computes absolute addresses
  from host-supplied page bases. See the "Host-side C" section below.

`firmware_program_fir`
: The C firmware (`plover_program_fir`) writes a coefficient table to
  the FIR through the host's AXI-Lite master, reads each tap back to
  confirm, then the test sequence streams samples through the chain.
  The DSP-aware scoreboard (described below) checks each output sample
  bit-exactly against the bit-exact Python chain model — and because
  the AXI-Lite passive monitor sees the C's coefficient writes as
  ordinary bus events, the scoreboard's model is kept in lock-step
  automatically. This is the OpenTitan-style invariant: the scoreboard
  sees software- and sequencer-driven traffic identically.

`chain_impulse`
: DSP-aware test. Programs the FIR as a delta filter (coef[0] max,
  rest zero), drives an impulse stream, and the scoreboard verifies
  the chain output is the bit-exact CIC impulse response.

`chain_tone`
: DSP-aware test. Programs the FIR as a unity-gain averager, drives a
  sinusoidal stream (parameterised: frequency, amplitude, length), and
  the scoreboard verifies the chain output sample-for-sample. The
  sequence carries signal info (`freq_norm`, `amplitude_frac`,
  `num_inputs`) — adding more signal types (chirps, noise, multi-tone)
  is a small addition.

The integration scoreboard is *DSP-aware*: it maintains a `CicFirChain`
Python reference model (a composition of the verified `CicDecimator`
and `FirFilter` primitives) and:

1. Watches AXI-Lite monitor for writes to the FIR page → updates the
   model's coefficient bank in lock-step.
2. Watches AXIS-in monitor for input samples → feeds them to the model.
3. Watches AXIS-out monitor for output samples → compares each against
   the model's prediction.

A bug-injection that tied the CIC→FIR interconnect to zero produced
"chain scoreboard: 63 mismatch(es); first: idx=0 expected=9 got=0";
breaking the FIR's AXI-Lite address routing produced "62 mismatch(es)"
on the same test. The scoreboard catches integration regressions that
the unit DVs cannot (each unit works in isolation; the chain test
catches wiring bugs between them).

**Software controls the counter.** `axil_shell.CONTROL.ENABLE` (bit 0
of register `0x04`) is now plumbed through to `u_counter.enable` in
`plover.sv`. After reset the bit is 0 (counter held), so the first
thing software does is write `CONTROL.ENABLE = 1` to start the
counter. The integration smoke test exercises the full cycle:

* Pre-enable, counter held at 0 (regression catch: any wiring that
  ties `counter_enable` high will fail this check immediately).
* Write `ENABLE=1`, counter advances ten cycles in ten clocks.
* Write `ENABLE=0`, counter freezes for ten cycles (count unchanged).
* Re-enable for the soft-reset gating test that follows.

The 31 spare CONTROL bits (`SPARE[31:1]`) are exposed from `axil_shell`
as a `control_spare[30:0]` port and routed up but unused at the top —
they're available for future use (e.g. one bit could drive
`u_counter.clear`).

The synthesis scaffolding under `top/syn/` is intentionally
vendor-agnostic at this stage; see `top/syn/README.md` for how to wire a
real synthesis flow into the `syn` target of `plover.core` once a vendor is
picked.

## Fixed-point format

Every DSP unit (`cic_decimator`, `cic_interpolator`, `fir_filter`) and
the project top expose paired parameters for each signed signal:

| Parameter           | Meaning                                              |
| ------------------- | ---------------------------------------------------- |
| `<sig>_W`           | Total signed width in bits (including the sign bit). |
| `<sig>_INT_W`       | Integer bits above the binary point (incl. sign).    |
| `<sig>_FRAC_W`      | Fractional bits below the binary point.              |

The three are related by `<sig>_W = <sig>_INT_W + <sig>_FRAC_W`; an
elaboration-time `$fatal` assertion in each unit catches mismatches.
The fractional/integer split says nothing about the *arithmetic* — all
units operate on plain signed integers internally — it only documents
the *interpretation*: a 16-bit signed integer can represent any
`Qm.n` format with `m+n=16`, and which one the testbench has in mind
is now visible at the instantiation site.

Defaults are `Q1.(W-1)` for every signal: one integer bit (the sign)
and `W-1` fractional bits, putting values in `[-1.0, +1.0)`. This
matches every existing test in the repo and means existing
instantiations work unchanged.

Worked example. With FIR defaults `COEF_W=16, COEF_INT_W=1,
COEF_FRAC_W=15`, the coefficients are `Q1.15`: the largest positive
value is `0x7FFF ≈ 0.99997`. With `COEF_INT_W=3, COEF_FRAC_W=13`, the
coefficients are `Q3.13`: the largest positive value is `0x7FFF ≈
3.99988`, giving headroom for filters whose impulse response sums to
> 1 without overflow. The FIR's `OUT_SHIFT` defaults to `COEF_FRAC_W`,
which preserves the input's Q-position through the multiply-and-
accumulate regardless of the coefficient Q-format.

The `fir_filter` unit's parameter sweep includes a `Q3.13` config
(`PARAM_CONFIGS` in `test_fir_filter_pytest.py`) that programs the
RTL and the Python reference model with matching `COEF_INT_W=3,
COEF_FRAC_W=13, OUT_SHIFT=13` and confirms bit-exact agreement
end-to-end — proving the new machinery actually drives a real Q
change and isn't just a documentation rename. The other configs
(default + the `T16`, `T4` sweeps) all stay at `Q1.15`.

CIC's arithmetic is intrinsically Q-position-preserving (the
top-OUT_W-bits truncation drops LSBs equally in input and output Q
positions), so its `_INT_W` / `_FRAC_W` parameters are purely
informational. They exist to make the contract legible and to let
downstream units (e.g. an FIR fed by the CIC) assert their inputs
match what's being produced.

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

### Two firmware tests live on this stack

`firmware_smoke`
: The minimum useful check: read the shell ID and the syscon VERSION
  through the single host_ops callbacks and confirm both match. Proves
  the chain C → Python → cocotb → Verilator → RTL → back works end-to-end.
  Bug-injection on either expected value fails the test loudly.

`firmware_program_fir`
: The C firmware uses the same single host_ops master to walk a
  coefficient table and write each tap to the FIR's AXI-Lite bank
  (`plover_program_fir`). Optionally reads each tap back to confirm
  the value committed. The cocotb side then streams samples through
  the chain, and the DSP-aware integration scoreboard (in
  `plover_env.py`) verifies the chain's output bit-exactly against a
  Python `CicFirChain` model — automatically, because the scoreboard's
  AXI-Lite passive monitor sees the C's coefficient writes as ordinary
  bus events and updates the model in lock-step. Software- and
  sequencer-driven coefficient programming are indistinguishable to
  the scoreboard, which is the OpenTitan-style invariant the project's
  agent design buys.

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
