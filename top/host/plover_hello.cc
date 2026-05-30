// =============================================================================
// plover_hello.cc — implementation of host-side test routines.
//
// Keep this file portable C++: no platform headers, no Python bindings, no
// simulator hooks. All host coupling is through the plover_host_ops struct
// of function pointers. That way the same .cc can later cross-compile to
// run on a real CPU board with a different read/write implementation.
// =============================================================================
#include "plover_hello.h"

#include <cstdarg>
#include <cstdio>

namespace {

// Known constants in the design under test. These match the RTL and are not
// supposed to change without a deliberate code change to the chip itself.
constexpr uint32_t kShellIdOffset   = 0x0C;
constexpr uint32_t kShellIdExpected = 0xC0C07B01u;
constexpr uint32_t kSysconVersionOffset = 0x00;

// Logger helper: if the host provided one, use it; otherwise drop the
// message on the floor. Local format buffer is small on purpose — anything
// that needs more than this should be split into multiple log calls.
void log_msg(const plover_host_ops* ops, const char* fmt, ...) {
    if (!ops || !ops->log) {
        return;
    }
    char buf[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    ops->log(buf);
}

}  // namespace

extern "C" int plover_hello_world(const plover_host_ops* ops,
                                  uint32_t expected_syscon_version) {
    if (!ops || !ops->shell_read || !ops->syscon_read) {
        return -1;  // misconfigured host
    }

    log_msg(ops, "plover firmware: reading axil_shell ID at 0x%02x",
            kShellIdOffset);
    const uint32_t id = ops->shell_read(kShellIdOffset);
    if (id != kShellIdExpected) {
        log_msg(ops, "FAIL: shell ID = 0x%08x, expected 0x%08x",
                id, kShellIdExpected);
        return 1;
    }
    log_msg(ops, "OK: shell ID = 0x%08x", id);

    log_msg(ops, "plover firmware: reading syscon VERSION at 0x%02x",
            kSysconVersionOffset);
    const uint32_t version = ops->syscon_read(kSysconVersionOffset);
    if (version != expected_syscon_version) {
        log_msg(ops, "FAIL: syscon VERSION = 0x%08x, expected 0x%08x",
                version, expected_syscon_version);
        return 2;
    }
    log_msg(ops, "OK: syscon VERSION = 0x%08x", version);

    log_msg(ops, "plover firmware: hello world complete");
    return 0;
}
