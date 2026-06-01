"""
Counter agent for the unit-test template, built on the dv_lib base classes.

The shape is intentionally minimal — just enough to be a faithful template for
a real sub-unit DV. To adapt:

1. Replace :class:`CounterItem` fields with your block's per-cycle stimulus
   (and observed outputs, if you want the monitor to ride the same item).
2. Update :class:`CounterDriver.drive_item` to write your block's inputs.
3. Update :class:`CounterMonitor.collect_trans` to sample your block's
   outputs on the active clock edge and broadcast via the analysis port.

The cfg holds the cocotb DUT handle as the ``vif`` (virtual interface), so
the driver and monitor get a live handle through ConfigDB the same way the
top-level shell's agent does.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import cocotb
from cocotb.triggers import RisingEdge, ReadOnly
from pyuvm import uvm_analysis_port

from dv_lib import (
    DVBaseAgent, DVBaseAgentCfg, DVBaseDriver, DVBaseMonitor, DVBaseSeqItem,
)


# ---- Sequence item --------------------------------------------------

@dataclass
class _ObservedCount:
    """Snapshot of the counter at a given cycle, written by the monitor."""
    cycle: int = 0
    count: int = 0


class CounterItem(DVBaseSeqItem):
    """One cycle of stimulus for the counter, plus an observation slot.

    The driver consumes ``clear``/``enable`` and asserts them on the next
    rising edge. The monitor populates ``observed`` after the edge so the
    scoreboard sees a single, consistent record per cycle.
    """

    def __init__(self, name: str = "counter_item",
                 clear: bool = False, enable: bool = True):
        super().__init__(name)
        self.clear = clear
        self.enable = enable
        self.observed: Optional[_ObservedCount] = None


# ---- Agent cfg ------------------------------------------------------

class CounterAgentCfg(DVBaseAgentCfg):
    """vif is the cocotb DUT handle. ``width`` matches the RTL parameter."""

    def __init__(self, name: str = "counter_agent_cfg") -> None:
        super().__init__(name)
        self.vif: Optional[Any] = None
        self.width: int = 8
        self.reset_signal_name: str = "rst_n"
        self.reset_active_low: bool = True


# ---- Driver ---------------------------------------------------------

class CounterDriver(DVBaseDriver):
    """Drives one item per clock cycle.

    dv_lib's base owns the get_next_item / item_done loop; we fill in
    :meth:`reset_signals` to idle the inputs during reset and
    :meth:`drive_item` to assert ``clear``/``enable`` synchronous to clk.
    Every driven item is also published on :attr:`stim_ap` so the scoreboard
    can advance its golden model in lockstep with the observed outputs.
    """

    def build_phase(self) -> None:
        super().build_phase()
        self.stim_ap = uvm_analysis_port("stim_ap", self)

    async def reset_signals(self) -> None:
        if self.cfg is None or self.cfg.vif is None or not cocotb.is_simulation:
            return
        dut = self.cfg.vif
        dut.clear.value = 0
        dut.enable.value = 0

    async def drive_item(self, item: CounterItem) -> None:
        if self.cfg is None or self.cfg.vif is None or not cocotb.is_simulation:
            self.stim_ap.write(item)
            return
        dut = self.cfg.vif
        # Set inputs, then consume one clock edge so the DUT registers them
        # this cycle. The monitor samples after the same edge and sees the
        # resulting count, so each driven item maps to one observation.
        dut.clear.value = int(item.clear)
        dut.enable.value = int(item.enable)
        await RisingEdge(dut.clk)
        self.stim_ap.write(item)


# ---- Monitor --------------------------------------------------------

class CounterMonitor(DVBaseMonitor):
    """Samples ``count`` one cycle after each driven stimulus and broadcasts.

    For a simple sub-unit it's clean to have the monitor own the cycle counter
    and emit one observation per cycle. A real block might sample on a valid
    handshake instead; this is the shape, not the only choice.
    """

    async def collect_trans(self) -> None:
        if self.cfg is None or self.cfg.vif is None or not cocotb.is_simulation:
            return
        dut = self.cfg.vif
        # Wait for reset deassertion before sampling, so the obs stream
        # starts when the driver's first item lands — otherwise the
        # scoreboard would pair reset-window observations (count=0) with
        # post-reset stimulus and report a phantom skew.
        rst = getattr(dut, self.cfg.reset_signal_name)
        while True:
            await RisingEdge(dut.clk)
            if (self.cfg.reset_active_low and rst.value == 1) or \
               (not self.cfg.reset_active_low and rst.value == 0):
                break

        cycle = 0
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()  # let all FFs settle before sampling
            obs = _ObservedCount(cycle=cycle, count=int(dut.count.value))
            cycle += 1
            self.write_and_sample(obs)


# ---- Agent ----------------------------------------------------------

class CounterAgent(DVBaseAgent):
    cfg_type = CounterAgentCfg
    driver_type = CounterDriver
    monitor_type = CounterMonitor
