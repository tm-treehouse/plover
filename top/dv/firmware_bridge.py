"""
Loader + cocotb bridge for the C host-side firmware library.

After the xbar refactor at the project top, all peripheral access goes
through a single host-side AXI-Lite master. The C side has correspondingly
collapsed to one read(addr) / write(addr) pair (no more shell_* vs
syscon_*); page bases are passed in alongside the version expectation so
the firmware can address each peripheral by its absolute bus address.

This module:

* Builds and loads ``libplover_hello.so`` via ctypes.
* Wraps the single cocotb ``AxiLiteMaster`` in C-callable read/write
  callbacks using cocotb 2.x's :func:`cocotb.task.bridge` /
  :func:`cocotb.task.resume`. The C thread blocks on each callback while
  the cocotb event loop services exactly one bus transaction.
* Exposes :func:`run_hello_world` which the test calls with ``await``.

Why this looks the way it does
------------------------------

The C side is synchronous; cocotb is async. The bridge between them is
``cocotb.task.bridge`` (wraps a sync function so it's awaitable, runs it in a
thread) and ``cocotb.task.resume`` (lets that sync function call back into
async cocotb code, blocking until the cocotb coroutine completes). Without
this dance, C calling Python which calls ``master.read()`` would either
deadlock or run the simulator and the test in parallel — neither is OK.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
from pathlib import Path

from cocotb.task import bridge, resume
from cocotbext.axi import AxiLiteMaster


_log = logging.getLogger("plover.firmware_bridge")

HERE = Path(__file__).resolve().parent
HOST_DIR = HERE.parent / "host"
LIB_PATH = HOST_DIR / "libplover_hello.so"


# ---- ctypes types --------------------------------------------------------

_ReadFn  = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32)
_WriteFn = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32)
_LogFn   = ctypes.CFUNCTYPE(None, ctypes.c_char_p)


class _HostOps(ctypes.Structure):
    """Mirror of plover_host_ops in plover_hello.h (single read/write pair)."""
    _fields_ = [
        ("read",  _ReadFn),
        ("write", _WriteFn),
        ("log",   _LogFn),
    ]


# ---- Library load + build-on-demand --------------------------------------

def _ensure_built(include_dirs: list[Path] | None = None) -> Path:
    """Build the .so. Returns the path.

    Unconditional rebuild: this is the path through which the FuseSoC-built
    generated headers (axil_shell_regs.h, syscon_regs.h) reach the C
    compile, and those headers live under different build-dir paths each
    time the harness runs. Skipping the rebuild based purely on source
    mtimes would silently reuse a .so compiled against stale headers when
    the build dir changes. The compile takes <1s so always rebuilding is
    the honest move.
    """
    env = os.environ.copy()
    if include_dirs:
        env["EXTRA_INCLUDES"] = " ".join(f"-I{d}" for d in include_dirs)
    _log.info(f"building {LIB_PATH.name} "
              f"(includes: {env.get('EXTRA_INCLUDES', 'none')})")
    subprocess.run(["make", "-C", str(HOST_DIR), "clean"], check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(["make", "-C", str(HOST_DIR)], check=True, env=env)
    return LIB_PATH


def _load(include_dirs: list[Path] | None = None) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(_ensure_built(include_dirs)))
    # int plover_hello_world(const plover_host_ops* ops,
    #                        uint32_t shell_base,
    #                        uint32_t syscon_base,
    #                        uint32_t expected_syscon_version)
    lib.plover_hello_world.argtypes = [
        ctypes.POINTER(_HostOps),
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ]
    lib.plover_hello_world.restype = ctypes.c_int
    return lib


# ---- Bridge: sync callbacks that delegate to the async AXI master --------

def _make_callbacks(host: AxiLiteMaster):
    """Build C-callable read/write callbacks bound to the given master.

    The async ``master.read`` / ``master.write`` coroutines are wrapped with
    ``@resume`` so they can be called from the bridge thread (where C is
    running). The C-callable wrappers are plain Python functions; ctypes
    handles the C↔Python conversion.
    """
    byte_lanes = host.write_if.byte_lanes  # 4 for 32-bit AXI-Lite

    @resume
    async def _aread(addr: int) -> int:
        resp = await host.read(addr, byte_lanes)
        return int.from_bytes(resp.data, "little")

    @resume
    async def _awrite(addr: int, data: int) -> None:
        await host.write(addr, (data & 0xFFFFFFFF).to_bytes(byte_lanes, "little"))

    def read_cb(addr: int) -> int:
        return _aread(int(addr))

    def write_cb(addr: int, data: int) -> None:
        _awrite(int(addr), int(data))

    def log_cb(msg_bytes) -> None:
        msg = msg_bytes.decode("utf-8", errors="replace") if msg_bytes else ""
        _log.info(msg)

    return read_cb, write_cb, log_cb


# ---- Public entry --------------------------------------------------------

@bridge
def _call_hello_world(lib, ops_ptr,
                      shell_base: int, syscon_base: int,
                      expected_version: int) -> int:
    """Sync wrapper around the C entry point. The body runs in a bridge
    thread; any read/write callback inside the C code blocks here while the
    cocotb event loop services the AXI transaction, then we return.
    """
    return int(lib.plover_hello_world(
        ops_ptr,
        ctypes.c_uint32(shell_base),
        ctypes.c_uint32(syscon_base),
        ctypes.c_uint32(expected_version)))


async def run_hello_world(host: AxiLiteMaster, *,
                          shell_base: int,
                          syscon_base: int,
                          expected_syscon_version: int,
                          include_dirs: list[Path] | None = None) -> int:
    """Run the C hello-world test against the single AXI master.

    ``shell_base`` / ``syscon_base`` are the page base addresses the xbar
    uses at the top boundary. The C firmware combines these with the
    register offsets it knows from the peakrdl-cheader headers.
    """
    lib = _load(include_dirs)
    read_cb, write_cb, log_cb = _make_callbacks(host)

    # Keep references alive — ctypes' callback wrappers are GC'd otherwise.
    ops = _HostOps(
        read  = _ReadFn(read_cb),
        write = _WriteFn(write_cb),
        log   = _LogFn(log_cb),
    )

    rc = await _call_hello_world(
        lib, ctypes.byref(ops),
        shell_base, syscon_base,
        expected_syscon_version)
    return rc
