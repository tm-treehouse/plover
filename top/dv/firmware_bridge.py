"""
Loader + cocotb bridge for the C++ host-side firmware library.

The C++ code in ``top/host/`` exposes test routines that operate on the chip
through callbacks (read/write a 32-bit register on either AXI slave). This
module:

* Builds and loads ``libplover_hello.so`` via ctypes.
* Wraps two cocotb ``AxiLiteMaster`` instances (shell + syscon) in C callable
  callbacks using cocotb 2.x's :func:`cocotb.task.bridge` /
  :func:`cocotb.task.resume`. The C++ thread blocks on each callback while
  the cocotb event loop advances the simulation by exactly one bus
  transaction, then returns the result to C++.
* Exposes :func:`run_hello_world` which the test calls with ``await``.

Why this looks the way it does
------------------------------

The C side is synchronous; cocotb is async. The bridge between them is
``cocotb.task.bridge`` (wraps a sync function so it's awaitable, runs it in a
thread) and ``cocotb.task.resume`` (lets that sync function call back into
async cocotb code, blocking until the cocotb coroutine completes). Without
this dance, C++ calling Python which calls ``master.read()`` would either
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
    _fields_ = [
        ("shell_read",   _ReadFn),
        ("shell_write",  _WriteFn),
        ("syscon_read",  _ReadFn),
        ("syscon_write", _WriteFn),
        ("log",          _LogFn),
    ]


# ---- Library load + build-on-demand --------------------------------------

def _ensure_built() -> Path:
    """Build the .so if missing or older than its sources. Returns the path."""
    srcs = [HOST_DIR / "plover_hello.cc", HOST_DIR / "plover_hello.h",
            HOST_DIR / "Makefile"]
    needs_build = not LIB_PATH.exists() or any(
        s.stat().st_mtime > LIB_PATH.stat().st_mtime
        for s in srcs if s.exists()
    )
    if needs_build:
        _log.info(f"building {LIB_PATH.name} (sources newer than artifact)")
        subprocess.run(["make", "-C", str(HOST_DIR)], check=True)
    return LIB_PATH


def _load() -> ctypes.CDLL:
    lib = ctypes.CDLL(str(_ensure_built()))
    lib.plover_hello_world.argtypes = [ctypes.POINTER(_HostOps), ctypes.c_uint32]
    lib.plover_hello_world.restype = ctypes.c_int
    return lib


# ---- Bridge: sync callbacks that delegate to async AXI masters -----------

def _make_callbacks(shell: AxiLiteMaster, syscon: AxiLiteMaster):
    """Build C-callable read/write callbacks bound to the given masters.

    The async ``master.read`` / ``master.write`` coroutines are wrapped with
    ``@resume`` so they can be called from the bridge thread (where C++ is
    running). The C-callable wrappers are plain Python functions; ctypes
    handles the C↔Python conversion.
    """

    byte_lanes = shell.write_if.byte_lanes  # 4 for 32-bit AXI-Lite

    @resume
    async def _aread(master: AxiLiteMaster, addr: int) -> int:
        resp = await master.read(addr, byte_lanes)
        return int.from_bytes(resp.data, "little")

    @resume
    async def _awrite(master: AxiLiteMaster, addr: int, data: int) -> None:
        await master.write(addr, (data & 0xFFFFFFFF).to_bytes(byte_lanes, "little"))

    def shell_read(addr: int) -> int:
        return _aread(shell, int(addr))

    def shell_write(addr: int, data: int) -> None:
        _awrite(shell, int(addr), int(data))

    def syscon_read(addr: int) -> int:
        return _aread(syscon, int(addr))

    def syscon_write(addr: int, data: int) -> None:
        _awrite(syscon, int(addr), int(data))

    def log_cb(msg_bytes) -> None:
        msg = msg_bytes.decode("utf-8", errors="replace") if msg_bytes else ""
        _log.info(msg)

    return shell_read, shell_write, syscon_read, syscon_write, log_cb


# ---- Public entry --------------------------------------------------------

@bridge
def _call_hello_world(lib, ops_ptr, expected_version: int) -> int:
    """Sync wrapper around the C entry point. The body runs in a bridge
    thread; any read/write callback inside the C code blocks here while the
    cocotb event loop services the AXI transaction, then we return.
    """
    return int(lib.plover_hello_world(ops_ptr, ctypes.c_uint32(expected_version)))


async def run_hello_world(shell: AxiLiteMaster, syscon: AxiLiteMaster,
                          expected_syscon_version: int) -> int:
    """Run the C++ hello-world test against the two AXI masters."""
    lib = _load()
    cb = _make_callbacks(shell, syscon)
    shell_read, shell_write, syscon_read, syscon_write, log_cb = cb

    # Keep references alive — ctypes' callback wrappers are GC'd otherwise.
    ops = _HostOps(
        shell_read   = _ReadFn(shell_read),
        shell_write  = _WriteFn(shell_write),
        syscon_read  = _ReadFn(syscon_read),
        syscon_write = _WriteFn(syscon_write),
        log          = _LogFn(log_cb),
    )

    rc = await _call_hello_world(lib, ctypes.byref(ops), expected_syscon_version)
    return rc
