"""
Shared AXI-Stream master agent for plover unit testbenches.

The driver drives a downstream AXI-Stream slave by pushing beats through
cocotbext-axi's :class:`AxiStreamSource`. The monitor is a passive bus
sampler — it watches TVALID/TREADY on the configured port and publishes
one :class:`AxiStreamItem` per accepted beat. The driver and monitor are
independent: the monitor doesn't care which master issued the traffic,
which is the OpenTitan-style invariant ("scoreboard's view comes from
the bus, not from any source of stimulus").

Components:
* :class:`AxiStreamItem`     — one beat (data).
* :class:`AxiStreamAgentCfg` — vif + signal prefix + byte-lane count.
* :class:`AxiStreamDriver`   — builds an ``AxiStreamSource`` on first run
                                and maps each item onto a ``source.send()``
                                call. No mirror — the monitor is the
                                source of truth.
* :class:`AxiStreamMonitor`  — samples the bus each cycle, publishes one
                                item per (TVALID && TREADY) handshake.
* :class:`AxiStreamAgent`    — wires them together via the dv_lib base.

See ``axi_lite_agent.py`` for the rationale for this living under /dv/
rather than in pyuvm-dv-lib or tools/.
"""
from __future__ import annotations

from typing import Any, Optional

import cocotb

from dv_lib import (
    DVBaseAgent, DVBaseAgentCfg, DVBaseDriver, DVBaseMonitor, DVBaseSeqItem,
)


# ---- Sequence item --------------------------------------------------

class AxiStreamItem(DVBaseSeqItem):
    """One AXI-Stream beat.

    ``data`` is the TDATA payload as an integer.

    There's no explicit ``tlast`` field here: cocotbext-axi's
    :class:`AxiStreamSource` treats each ``send(bytes)`` call as a
    single-beat frame and asserts TLAST automatically on that beat. If a
    future testbench needs multi-beat frames with TLAST only on the last
    beat, extend the driver to accept ``AxiStreamFrame`` objects directly.
    """

    def __init__(self, name: str = "axi_stream_item", data: int = 0):
        super().__init__(name)
        self.data = data


# ---- Agent cfg ------------------------------------------------------

class AxiStreamAgentCfg(DVBaseAgentCfg):
    """vif = cocotb DUT handle; prefix = signal-name prefix the BFM matches.

    ``byte_lanes`` is derived from the DUT's TDATA width at agent build time
    if left at its default of None; tests can override to force a specific
    width.
    """

    def __init__(self, name: str = "axi_stream_agent_cfg") -> None:
        super().__init__(name)
        self.vif: Optional[Any] = None
        self.prefix: str = "s_axis"
        self.reset_signal_name: str = "rst_n"
        self.reset_active_low: bool = True
        self.byte_lanes: Optional[int] = None


# ---- Driver ---------------------------------------------------------

class AxiStreamDriver(DVBaseDriver):
    """Drives AXI-Stream items onto the DUT via cocotbext-axi's source BFM.

    No analysis-port mirror — the :class:`AxiStreamMonitor` samples the
    bus directly. That matches the OpenTitan dv_lib model and means the
    scoreboard sees beats consistently whether they were issued via the
    sequencer or by some other path.
    """

    def __init__(self, name: str = "axi_stream_driver", parent=None) -> None:
        super().__init__(name, parent)
        self._source = None
        self._byte_lanes: int = 0

    def _ensure_source(self):
        if self._source is not None:
            return self._source
        # Late import: avoids requiring cocotbext-axi at module-import time.
        from cocotbext.axi import AxiStreamBus, AxiStreamSource
        assert self.cfg is not None and self.cfg.vif is not None, \
            "axi-stream agent vif (DUT handle) not configured"
        dut = self.cfg.vif
        reset = getattr(dut, self.cfg.reset_signal_name)
        bus = AxiStreamBus.from_prefix(dut, self.cfg.prefix)
        self._source = AxiStreamSource(
            bus, dut.clk, reset,
            reset_active_level=not self.cfg.reset_active_low,
        )
        # Resolve byte lanes from the DUT's TDATA width if not overridden.
        if self.cfg.byte_lanes is not None:
            self._byte_lanes = self.cfg.byte_lanes
        else:
            tdata = getattr(dut, f"{self.cfg.prefix}_tdata")
            self._byte_lanes = len(tdata) // 8
        return self._source

    async def drive_item(self, item: AxiStreamItem) -> None:
        if not cocotb.is_simulation:
            return
        source = self._ensure_source()
        payload = (item.data & ((1 << (self._byte_lanes * 8)) - 1)) \
            .to_bytes(self._byte_lanes, "little")
        # send(bytes) sends a one-beat frame with TLAST asserted on the
        # beat. That matches what the DUTs in this project expect (TLAST
        # is either ignored or accepted as a beat marker).
        await source.send(payload)


# ---- Monitor --------------------------------------------------------

class AxiStreamMonitor(DVBaseMonitor):
    """Passive bus-sampling monitor for AXI-Stream.

    Watches TVALID && TREADY on the configured port every clock; on
    each accepted beat, publishes an :class:`AxiStreamItem` with the
    observed TDATA. Because it samples the bus, it sees beats regardless
    of which master drove them.

    Reset behaviour: while reset is asserted, sampling is skipped. Since
    the monitor has no internal state between beats (one item per
    accepted beat, no pairing across cycles), no flush is needed — just
    don't sample.
    """

    async def collect_trans(self) -> None:
        if not cocotb.is_simulation or self.cfg is None or self.cfg.vif is None:
            return
        from cocotb.triggers import RisingEdge, ReadOnly

        dut = self.cfg.vif
        prefix = self.cfg.prefix
        tdata  = getattr(dut, f"{prefix}_tdata")
        tvalid = getattr(dut, f"{prefix}_tvalid")
        tready = getattr(dut, f"{prefix}_tready")
        rst    = getattr(dut, self.cfg.reset_signal_name)

        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            # Skip sampling during reset (signals may be X or
            # mid-transaction).
            v = int(rst.value)
            in_reset = (v == 0) if self.cfg.reset_active_low else (v == 1)
            if in_reset:
                continue
            if int(tvalid.value) and int(tready.value):
                item = AxiStreamItem(data=int(tdata.value))
                if self.analysis_port is not None:
                    self.analysis_port.write(item)


# ---- Agent ----------------------------------------------------------

class AxiStreamAgent(DVBaseAgent):
    cfg_type = AxiStreamAgentCfg
    driver_type = AxiStreamDriver
    monitor_type = AxiStreamMonitor
