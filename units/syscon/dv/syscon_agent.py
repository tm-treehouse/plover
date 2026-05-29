"""AXI-Lite agent for the syscon unit testbench, on the dv_lib base classes.

Identical in shape to the axil_shell agent — same item, same cfg fields, same
cocotbext-axi driver, same mirror-based monitor. Kept as a separate file
rather than a shared one because each block's RDL and reference model are
different, and the agent is small enough that duplication is honest.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pyuvm import uvm_analysis_port

import cocotb

from dv_lib import (
    DVBaseAgent, DVBaseAgentCfg, DVBaseDriver, DVBaseMonitor, DVBaseSeqItem,
)


class SysconOp(Enum):
    READ = 0
    WRITE = 1


class SysconItem(DVBaseSeqItem):
    def __init__(self, name: str = "syscon_item",
                 op: SysconOp = SysconOp.READ, addr: int = 0, data: int = 0):
        super().__init__(name)
        self.op = op
        self.addr = addr
        self.data = data
        self.resp = 0


class SysconAgentCfg(DVBaseAgentCfg):
    def __init__(self, name: str = "syscon_agent_cfg") -> None:
        super().__init__(name)
        self.vif: Optional[Any] = None
        self.prefix: str = "s_axil"
        self.reset_signal_name: str = "rst_n"
        self.reset_active_low: bool = True


class SysconDriver(DVBaseDriver):
    def __init__(self, name: str = "syscon_driver", parent=None) -> None:
        super().__init__(name, parent)
        self._mirror_port: Optional[uvm_analysis_port] = None
        self._master = None

    def set_mirror_port(self, port: uvm_analysis_port) -> None:
        self._mirror_port = port

    def _ensure_master(self):
        if self._master is not None:
            return self._master
        from cocotbext.axi import AxiLiteBus, AxiLiteMaster
        assert self.cfg is not None and self.cfg.vif is not None, \
            "syscon agent vif (DUT handle) not configured"
        dut = self.cfg.vif
        reset = getattr(dut, self.cfg.reset_signal_name)
        self._master = AxiLiteMaster(
            AxiLiteBus.from_prefix(dut, self.cfg.prefix),
            dut.clk,
            reset,
            reset_active_level=not self.cfg.reset_active_low,
        )
        return self._master

    async def drive_item(self, item: SysconItem) -> None:
        if not cocotb.is_simulation:
            self._mirror(item)
            return
        master = self._ensure_master()
        byte_lanes = master.write_if.byte_lanes
        if item.op is SysconOp.WRITE:
            data = item.data.to_bytes(byte_lanes, "little")
            resp = await master.write(item.addr, data)
            item.resp = int(resp.resp)
        else:
            resp = await master.read(item.addr, byte_lanes)
            item.data = int.from_bytes(resp.data, "little")
            item.resp = int(resp.resp)
        self._mirror(item)

    def _mirror(self, item: SysconItem) -> None:
        if self._mirror_port is not None:
            self._mirror_port.write(item)


class SysconMonitor(DVBaseMonitor):
    """Rebroadcasts mirrored items from the driver, same pattern as axil_shell."""
    pass


class SysconAgent(DVBaseAgent):
    cfg_type = SysconAgentCfg
    driver_type = SysconDriver
    monitor_type = SysconMonitor

    def connect_phase(self) -> None:
        super().connect_phase()
        if self.cfg is not None and self.cfg.active:
            assert self.driver is not None and self.monitor is not None
            assert self.monitor.analysis_port is not None
            self.driver.set_mirror_port(self.monitor.analysis_port)
