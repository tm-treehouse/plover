// =============================================================================
// plover_hello.h
//
// Host-side "firmware" library API. The C++ test routines here treat the
// chip as a register file behind two AXI4-Lite slaves and operate on it
// through callbacks supplied by the host environment (e.g. the cocotb
// testbench at the bottom of the simulator). The same source could later be
// cross-compiled and run on a real CPU mastering AXI on a board — the C++
// stays the same; only the read/write implementations change.
//
// Everything here is plain C ABI so ctypes (Python side) can load and call
// it without help.
// =============================================================================
#ifndef PLOVER_HELLO_H
#define PLOVER_HELLO_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Callback function pointer types. Each takes/returns a single 32-bit word
// at the given byte offset on the named AXI-Lite slave.
typedef uint32_t (*plover_read_fn) (uint32_t addr);
typedef void     (*plover_write_fn)(uint32_t addr, uint32_t data);

// Host environment passes one of these in when calling the test routines.
// shell_* talks to the axil_shell slave (s_axil_* at the plover top boundary)
// and syscon_* talks to the syscon slave (s_syscon_*).
typedef struct {
    plover_read_fn  shell_read;
    plover_write_fn shell_write;
    plover_read_fn  syscon_read;
    plover_write_fn syscon_write;
    // Optional logger: if non-NULL, the test routines call this with
    // human-readable progress strings. Bound to the cocotb logger by the
    // Python bridge.
    void (*log)(const char* msg);
} plover_host_ops;

// Returns 0 on success, non-zero on the first failing check.
//
// The current test does the minimum useful thing — reads two known-constant
// or known-overridden registers via both AXI paths, confirming the host
// callbacks really reach the chip:
//   * axil_shell ID register at 0x0C   (expected 0xC0C07B01)
//   * syscon VERSION register at 0x00  (expected value provided by caller)
//
// The expected version is supplied as an argument rather than hard-coded so
// the Python bridge can pass the same value it tells the Verilator build to
// use, keeping the C++ side simulator-agnostic.
int plover_hello_world(const plover_host_ops* ops, uint32_t expected_syscon_version);

#ifdef __cplusplus
}
#endif

#endif // PLOVER_HELLO_H
