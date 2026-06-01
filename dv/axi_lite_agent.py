"""
Shared AXI-Lite agent for plover unit testbenches.

The agent extends the pyuvm-dv-lib base classes (which port OpenTitan's
`dv_lib` to Python) with the AXI-Lite-specific bits this project needs:

* :class:`AxiLiteOp`     — READ / WRITE enum on the sequence item.
* :class:`AxiLiteItem`   — one AXI-Lite transaction.
* :class:`AxiLiteAgentCfg` — agent cfg holding the cocotb DUT handle in ``vif``
                            plus the signal prefix used by the BFM.
* :class:`AxiLiteDriver` — builds an ``AxiLiteMaster`` (cocotbext-axi) on first
                            run and maps each item onto a BFM read/write,
                            back-annotating ``data``/``resp`` on the item.
* :class:`AxiLiteMonitor` — rebroadcasts the driver's mirrored items.
* :class:`AxiLiteAgent`  — wires them together via the dv_lib base agent.

Why this lives in /dv/ rather than in pyuvm-dv-lib:
    pyuvm-dv-lib is a port of OpenTitan's *dv_lib* — just the base-class
    skeleton (agents, drivers, monitors, scoreboard, env, plusargs, report
    catcher). OpenTitan keeps protocol-specific agents (TileLink, etc.) in
    a separate *cip_lib*, which is not part of pyuvm-dv-lib. Protocol agents
    for this project belong in the project.

Why this lives in /dv/ rather than in tools/:
    tools/ holds build-time scripts (RDL generators, version header). The DV
    agents are runtime components consumed by the testbenches; mixing them
    with build-time tooling would muddy what each directory means.

Each unit's testbench imports from here:
    from dv.axi_lite_agent import (
        AxiLiteOp, AxiLiteItem, AxiLiteAgent, AxiLiteAgentCfg,
    )
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

import cocotb

from dv_lib import (
    DVBaseAgent, DVBaseAgentCfg, DVBaseDriver, DVBaseMonitor, DVBaseSeqItem,
)


# ---- Sequence item --------------------------------------------------

class AxiLiteOp(Enum):
    READ = 0
    WRITE = 1


class AxiLiteItem(DVBaseSeqItem):
    """One AXI-Lite transaction.

    For a WRITE: ``addr``/``data`` are inputs, ``resp`` is filled by the driver.
    For a READ:  ``addr`` is the input, ``data``/``resp`` are filled by the driver.
    """

    def __init__(self, name: str = "axi_lite_item",
                 op: AxiLiteOp = AxiLiteOp.READ, addr: int = 0, data: int = 0):
        super().__init__(name)
        self.op = op
        self.addr = addr
        self.data = data
        self.resp = 0


# ---- Agent cfg ------------------------------------------------------

class AxiLiteAgentCfg(DVBaseAgentCfg):
    """Adds the AXI-Lite-specific handles on top of the dv_lib defaults.

    ``vif`` is the cocotb DUT handle (or any object exposing the prefixed
    AXI-Lite signals). ``prefix`` is the signal-name prefix the BFM matches
    with ``AxiLiteBus.from_prefix`` (e.g. ``"s_axil"``).

    A single AXI-Lite slave at the DUT boundary is the common case. For a
    DUT with multiple slave ports (e.g. plover before the xbar consolidation),
    instantiate one agent per port with a different ``prefix``.
    """

    def __init__(self, name: str = "axi_lite_agent_cfg") -> None:
        super().__init__(name)
        self.vif: Optional[Any] = None      # cocotb DUT handle
        self.prefix: str = "s_axil"
        self.reset_signal_name: str = "rst_n"
        self.reset_active_low: bool = True


# ---- Driver ---------------------------------------------------------

class AxiLiteDriver(DVBaseDriver):
    """Drives AXI-Lite items onto the DUT via cocotbext-axi.

    The dv_lib base owns the get_next_item / item_done loop; we only fill
    in :meth:`drive_item`. There's no need for an explicit ``reset_signals``
    method here because cocotbext-axi's ``AxiLiteMaster`` idles its outputs
    while ``reset`` is asserted, and the testbench's clock/reset driver
    holds reset for several cycles before any item is issued.

    The driver back-annotates ``item.resp`` and (for reads) ``item.data``
    on each driven item, so the sequence body can read those after
    ``finish_item``. The driver does **not** mirror items to any analysis
    port — :class:`AxiLiteMonitor` samples the bus and is the sole source
    of observed transactions for the scoreboard. That separation matches
    the OpenTitan dv_lib model and means the scoreboard sees the same
    stream whether transactions came from a sequencer-driven test or
    from a bypass path (e.g. the project top's firmware bridge).
    """

    def __init__(self, name: str = "axi_lite_driver", parent=None) -> None:
        super().__init__(name, parent)
        self._master = None

    def ensure_master(self):
        """Build the underlying ``AxiLiteMaster`` if not yet built; return it.

        Lazy because cocotbext-axi's master expects to be constructed in
        a simulation context (it spawns coroutines on the clock). The
        first call from the cocotb run_phase builds it; subsequent calls
        return the same instance.

        Tests that need to talk to the BFM outside the sequencer (e.g.
        from a firmware bridge in C-thread context) call this once
        during run_phase to get a handle. For tests that only issue
        items through the sequencer it's a no-op — :meth:`drive_item`
        calls it on first use.
        """
        if self._master is not None:
            return self._master
        # Imported here so the module imports cleanly with no simulator
        # (e.g. running plain `pytest` to lint the dv code).
        from cocotbext.axi import AxiLiteBus, AxiLiteMaster
        assert self.cfg is not None and self.cfg.vif is not None, \
            "axi-lite agent vif (DUT handle) not configured"
        dut = self.cfg.vif
        reset = getattr(dut, self.cfg.reset_signal_name)
        self._master = AxiLiteMaster(
            AxiLiteBus.from_prefix(dut, self.cfg.prefix),
            dut.clk,
            reset,
            reset_active_level=not self.cfg.reset_active_low,
        )
        return self._master

    async def drive_item(self, item: AxiLiteItem) -> None:
        if not cocotb.is_simulation:
            # No simulator (e.g. plain pytest of dv modules): nothing to
            # drive, just leave the item as-is. The monitor isn't running
            # either, so the scoreboard won't see anything — that's the
            # right behaviour for plain unit tests of the dv code.
            return

        master = self.ensure_master()
        byte_lanes = master.write_if.byte_lanes
        if item.op is AxiLiteOp.WRITE:
            data = item.data.to_bytes(byte_lanes, "little")
            resp = await master.write(item.addr, data)
            item.resp = int(resp.resp)
        else:  # READ
            resp = await master.read(item.addr, byte_lanes)
            item.data = int.from_bytes(resp.data, "little")
            item.resp = int(resp.resp)


# ---- Monitor --------------------------------------------------------

class AxiLiteMonitor(DVBaseMonitor):
    """Passive bus-sampling monitor for AXI-Lite.

    Samples the bus every cycle, detects handshakes (VALID && READY on a
    rising edge) on each channel, reconstructs transactions, and writes
    completed :class:`AxiLiteItem` instances to ``self.analysis_port``.

    Independent read and write paths: AXI-Lite write transactions need
    AW, W, and B to all complete (in any order; cocotbext-axi pipelines
    them); reads need AR and R. We track each pair in its own coroutine
    and FIFO so writes and reads can be in flight simultaneously without
    interfering.

    Because this is passive, it sees transactions regardless of which
    master issued them — sequencer-driven items, items issued by a
    test's run_phase directly, items issued from a C-thread bridge, all
    look the same on the bus. That's the OpenTitan-style invariant: the
    scoreboard's view of "what happened" comes from the bus, not from
    any one source of stimulus.
    """

    async def collect_trans(self) -> None:
        if not cocotb.is_simulation or self.cfg is None or self.cfg.vif is None:
            return
        dut = self.cfg.vif
        prefix = self.cfg.prefix
        # Two independent coroutines for the W and R paths. Both run for
        # the lifetime of the simulation and publish items as they observe
        # complete transactions.
        cocotb.start_soon(self._watch_writes(dut, prefix))
        cocotb.start_soon(self._watch_reads(dut, prefix))

    @staticmethod
    def _sig(dut, prefix: str, name: str):
        return getattr(dut, f"{prefix}_{name}")

    def _in_reset(self) -> bool:
        """True iff the configured reset signal is currently asserted.

        Active-low reset (the default) is asserted when the signal value
        is 0; active-high is asserted when 1. Called after ReadOnly() so
        the signal is stable.
        """
        assert self.cfg is not None and self.cfg.vif is not None
        rst = getattr(self.cfg.vif, self.cfg.reset_signal_name)
        v = int(rst.value)
        return (v == 0) if self.cfg.reset_active_low else (v == 1)

    async def _watch_writes(self, dut, prefix: str) -> None:
        """Pair each AW with its W and B to publish one item per write.

        AXI-Lite writes are loosely ordered: AW and W can complete in
        either order, and B follows. We collect AW addresses in one
        queue and W payloads in another; pair them in arrival order to
        form a pending transaction; finalize with the B response.

        Reset behaviour: while reset is asserted, internal queues are
        flushed every cycle and no sampling happens. This prevents
        partially-observed transactions (e.g. an AW that was captured
        before reset hit, where the W never arrived) from pairing
        against unrelated post-reset traffic.
        """
        from cocotb.triggers import RisingEdge, ReadOnly
        from collections import deque

        awaddr  = self._sig(dut, prefix, "awaddr")
        awvalid = self._sig(dut, prefix, "awvalid")
        awready = self._sig(dut, prefix, "awready")
        wdata   = self._sig(dut, prefix, "wdata")
        wvalid  = self._sig(dut, prefix, "wvalid")
        wready  = self._sig(dut, prefix, "wready")
        bresp   = self._sig(dut, prefix, "bresp")
        bvalid  = self._sig(dut, prefix, "bvalid")
        bready  = self._sig(dut, prefix, "bready")

        aw_q: deque[int] = deque()
        w_q:  deque[int] = deque()
        pending: deque[AxiLiteItem] = deque()

        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if self._in_reset():
                aw_q.clear()
                w_q.clear()
                pending.clear()
                continue
            if int(awvalid.value) and int(awready.value):
                aw_q.append(int(awaddr.value))
            if int(wvalid.value) and int(wready.value):
                w_q.append(int(wdata.value))
            # Form pending transactions whenever an AW and W are both
            # available; FIFO order matches the bus's observed order
            # for the simple master we expect.
            while aw_q and w_q:
                item = AxiLiteItem(op=AxiLiteOp.WRITE,
                                    addr=aw_q.popleft(),
                                    data=w_q.popleft())
                pending.append(item)
            if int(bvalid.value) and int(bready.value) and pending:
                item = pending.popleft()
                item.resp = int(bresp.value)
                if self.analysis_port is not None:
                    self.analysis_port.write(item)

    async def _watch_reads(self, dut, prefix: str) -> None:
        """Pair each AR with its R to publish one item per read.

        Reset behaviour: same as :meth:`_watch_writes` — queues flushed
        every cycle reset is asserted.
        """
        from cocotb.triggers import RisingEdge, ReadOnly
        from collections import deque

        araddr  = self._sig(dut, prefix, "araddr")
        arvalid = self._sig(dut, prefix, "arvalid")
        arready = self._sig(dut, prefix, "arready")
        rdata   = self._sig(dut, prefix, "rdata")
        rresp   = self._sig(dut, prefix, "rresp")
        rvalid  = self._sig(dut, prefix, "rvalid")
        rready  = self._sig(dut, prefix, "rready")

        ar_q: deque[int] = deque()

        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if self._in_reset():
                ar_q.clear()
                continue
            if int(arvalid.value) and int(arready.value):
                ar_q.append(int(araddr.value))
            if int(rvalid.value) and int(rready.value) and ar_q:
                item = AxiLiteItem(op=AxiLiteOp.READ, addr=ar_q.popleft())
                item.data = int(rdata.value)
                item.resp = int(rresp.value)
                if self.analysis_port is not None:
                    self.analysis_port.write(item)


# ---- Agent ----------------------------------------------------------

class AxiLiteAgent(DVBaseAgent):
    cfg_type = AxiLiteAgentCfg
    driver_type = AxiLiteDriver
    monitor_type = AxiLiteMonitor
