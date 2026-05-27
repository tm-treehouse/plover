"""
AXI-Lite agent for the shell testbench, built on the dv_lib base classes.

This mirrors the structure of the dv_lib ALU example's input agent, but the
``vif`` here is a *real* cocotb DUT handle rather than a Python queue, and the
driver/monitor talk to it through Alex Forencich's ``cocotbext-axi`` AXI-Lite
bus-functional model.

Components:
* :class:`AxilItem`       — sequence item (one AXI-Lite read or write).
* :class:`AxilAgentCfg`   — agent cfg; ``vif`` holds the cocotb DUT handle,
                            plus the signal ``prefix`` and clock period.
* :class:`AxilDriver`     — builds an ``AxiLiteMaster`` on first run and maps
                            each item onto a BFM read/write, back-annotating
                            the result. Mirrors driven items to the monitor.
* :class:`AxilMonitor`    — rebroadcasts observed transactions (the dv_lib
                            ``write_and_sample`` path feeds the scoreboard +
                            coverage).
* :class:`AxilAgent`      — wires them together via the dv_lib base agent.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pyuvm import uvm_analysis_port

import cocotb

from dv_lib import (
    DVBaseAgent, DVBaseAgentCfg, DVBaseDriver, DVBaseMonitor, DVBaseSeqItem,
)


# ---- Sequence item --------------------------------------------------

class AxilOp(Enum):
    READ = 0
    WRITE = 1


class AxilItem(DVBaseSeqItem):
    """One AXI-Lite transaction.

    For a WRITE: ``addr``/``data`` are inputs, ``resp`` is filled by the driver.
    For a READ:  ``addr`` is the input, ``data``/``resp`` are filled by the driver.
    """

    def __init__(self, name: str = "axil_item",
                 op: AxilOp = AxilOp.READ, addr: int = 0, data: int = 0):
        super().__init__(name)
        self.op = op
        self.addr = addr
        self.data = data
        self.resp = 0


# ---- Agent cfg ------------------------------------------------------

class AxilAgentCfg(DVBaseAgentCfg):
    """Adds the AXI-Lite-specific handles on top of the dv_lib defaults.

    ``vif`` is the cocotb DUT handle (or any object exposing the prefixed
    AXI-Lite signals). ``prefix`` is the signal-name prefix the BFM matches
    with ``AxiLiteBus.from_prefix`` (e.g. ``"s_axil"``).
    """

    def __init__(self, name: str = "axil_agent_cfg") -> None:
        super().__init__(name)
        self.vif: Optional[Any] = None     # cocotb DUT handle
        self.prefix: str = "s_axil"
        self.reset_signal_name: str = "rst_n"
        self.reset_active_low: bool = True


# ---- Driver ---------------------------------------------------------

class AxilDriver(DVBaseDriver):
    """Drives AXI-Lite items onto the DUT via cocotbext-axi.

    The dv_lib base owns the get_next_item / item_done loop; we only fill in
    :meth:`reset_signals` (idle the master inputs) and :meth:`drive_item`.
    """

    def __init__(self, name: str = "axil_driver", parent=None) -> None:
        super().__init__(name, parent)
        self._mirror_port: Optional[uvm_analysis_port] = None
        self._master = None

    def set_mirror_port(self, port: uvm_analysis_port) -> None:
        self._mirror_port = port

    def _ensure_master(self):
        if self._master is not None:
            return self._master
        # Imported here so the module imports cleanly with no simulator.
        from cocotbext.axi import AxiLiteBus, AxiLiteMaster
        assert self.cfg is not None and self.cfg.vif is not None, \
            "axil agent vif (DUT handle) not configured"
        dut = self.cfg.vif
        reset = getattr(dut, self.cfg.reset_signal_name)
        self._master = AxiLiteMaster(
            AxiLiteBus.from_prefix(dut, self.cfg.prefix),
            dut.clk,
            reset,
            reset_active_level=not self.cfg.reset_active_low,
        )
        return self._master

    async def drive_item(self, item: AxilItem) -> None:
        if not cocotb.is_simulation:
            # No simulator (e.g. plain pytest): nothing to drive, but still
            # mirror the item so the analysis path can be exercised.
            self._mirror(item)
            return

        master = self._ensure_master()
        byte_lanes = master.write_if.byte_lanes
        if item.op is AxilOp.WRITE:
            data = item.data.to_bytes(byte_lanes, "little")
            resp = await master.write(item.addr, data)
            item.resp = int(resp.resp)
        else:  # READ
            resp = await master.read(item.addr, byte_lanes)
            item.data = int.from_bytes(resp.data, "little")
            item.resp = int(resp.resp)

        self._mirror(item)

    def _mirror(self, item: AxilItem) -> None:
        # Hand the completed transaction to the monitor's analysis port so the
        # scoreboard sees a single, consistent stream. In real multi-master
        # RTL the monitor would sample the bus directly instead.
        if self._mirror_port is not None:
            self._mirror_port.write(item)


# ---- Monitor --------------------------------------------------------

class AxilMonitor(DVBaseMonitor):
    """Rebroadcasts mirrored items.

    In a real testbench this would be ``async def collect_trans()`` sampling
    the AXI-Lite channels every clock and calling ``self.write_and_sample``.
    Here it relies on the driver mirror, matching the dv_lib ALU example.
    """
    pass


# ---- Agent ----------------------------------------------------------

class AxilAgent(DVBaseAgent):
    cfg_type = AxilAgentCfg
    driver_type = AxilDriver
    monitor_type = AxilMonitor

    def connect_phase(self) -> None:
        super().connect_phase()
        if self.cfg is not None and self.cfg.active:
            assert self.driver is not None and self.monitor is not None
            assert self.monitor.analysis_port is not None
            self.driver.set_mirror_port(self.monitor.analysis_port)
