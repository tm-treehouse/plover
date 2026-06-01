# `top/host/` — host-side C firmware

C "firmware" that drives the plover top through register reads and
writes. The same source could later cross-compile and run on a CPU
mastering AXI on a board; in simulation it runs against the cocotb
testbench via a ctypes bridge.

## Files

| File              | What it is                                       |
| ----------------- | ------------------------------------------------ |
| `plover_hello.h`  | Public API (two entry points).                   |
| `plover_hello.c`  | Implementations of the entries.                  |
| `Makefile`        | Builds `libplover_hello.so` for the bridge.      |

## Entry points

Two functions, both take a `plover_host_ops*` containing read/write/log
callbacks (the bridge's hook into cocotb's AXI master):

* **`plover_hello_world(ops, shell_base, syscon_base, expected_version)`**
  — register smoke. Reads `axil_shell.ID` and `syscon.VERSION` via
  the bus, confirms each matches expectations. Returns 0 on success,
  non-zero on the first failing check.

* **`plover_program_fir(ops, fir_base, coefs, n_taps, verify_readback)`**
  — walks a caller-supplied coefficient table and writes each tap to
  `fir_base + 4*i` via the bus. Optionally reads each back to confirm.
  Returns 0 on success, non-zero on the first failing readback.

The firmware is *standalone C* — no peakrdl-generated headers, no
project-specific includes. The plover register-file offsets it needs
(`SHELL_ID_OFFSET`, `SYSCON_VERSION_OFFSET`) are baked in as integer
constants. The FIR's coefficient bank is memory-style (just word
offsets `4*i`), no RDL.

This keeps the C portable: cross-compile it for an embedded host with
a real AXI master and only the read/write callback implementations
change.

## The bridge — how the C reaches the simulator

The C side is synchronous; cocotb is async. The bridge between them
lives in `top/dv/firmware_bridge.py` and uses cocotb 2.x's
`cocotb.task.bridge` / `cocotb.task.resume`:

* `@bridge` wraps a sync function so it's awaitable; the function
  runs in a separate thread.
* `@resume` lets that sync function call back into async cocotb code;
  it blocks the C thread until the cocotb coroutine completes.

The bridge:

1. Builds `libplover_hello.so` via the Makefile (lazy, cached).
2. Loads it via ctypes.
3. Wraps cocotb's `AxiLiteMaster.read` / `.write` in C-callable
   callbacks (via `_ReadFn` / `_WriteFn` `CFUNCTYPE`s).
4. Constructs a `_HostOps` struct (mirroring `plover_host_ops` in
   the .h) and passes it into the C function.

The C calls `ops->read(addr)` / `ops->write(addr, data)`; each call
blocks the C thread while cocotb services exactly one bus transaction.

## Why this layout

* **Single read/write pair.** All peripheral access goes through the
  top's single AXI-Lite slave (the xbar fans it to per-peripheral
  pages). The C side has correspondingly collapsed to one
  read/write pair — no more per-peripheral `shell_read` /
  `syscon_read` callbacks. Page bases are passed in alongside
  expected register values so the firmware computes absolute bus
  addresses.

* **No peakrdl-generated C headers in the firmware.** The earlier
  arc tried using `peakrdl-cpp` headers; it added a build dependency
  and made the C non-portable. The current code just bakes the
  handful of offsets it actually uses. If the register layout grows
  to dozens of registers, revisit — for now this is simpler.

* **Two integration tests live on this stack:**
  - `firmware_smoke` — calls `plover_hello_world`. The cocotb
    sequence does nothing else.
  - `firmware_program_fir` — calls `plover_program_fir` to program
    an averaging filter, then the cocotb sequence streams samples
    through the chain. The DSP-aware scoreboard verifies the chain
    output bit-exactly. Because the scoreboard observes the C's
    coefficient writes via its passive AXI-Lite monitor, it stays in
    lock-step with the C-programmed coefficients without any
    explicit synchronization.

## Useful properties

The bridge gives you "the same scoreboard that catches sequencer-
driven bugs also catches firmware-driven bugs" for free:

* Bug-injection in the C (wrong constant, off-by-one in the
  coefficient loop) shows up as scoreboard mismatches on the next
  sample because the model and the RTL see different writes.
* Bug-injection in the RTL (broken xbar routing, broken FIR address
  decode) shows up the same way for the same reason.
* The model and the RTL are kept in sync by the passive monitor,
  not by the test code, so adding new firmware functions doesn't
  require updating the scoreboard.
