"""Plover top env: two agents + a stream-side scoreboard.

The top is an integration testbench, not unit re-verification. The
sub-units (axil_shell, syscon, axil_xbar, stream_sink) each have their
own DV that verifies them at the unit level. This env exists to confirm
the integration is wired and alive, not to re-check protocol behaviour.

Two agents:
* :class:`AxiLiteAgent` on the single ``s_axil_*`` host port. Drives
  register reads/writes; the xbar inside the DUT routes by address.
* :class:`AxiStreamAgent` on ``s_axis_*`` for the stream_sink path.

Scoreboard scope:
* AxiStream side accumulates expected beat_count / data_xor (same model
  as the stream_sink unit DV). End-of-test sampling lives in the base
  test, mirroring the stream_sink pattern.
* AxiLite side just gathers a transaction count for diagnostics — the
  integration test doesn't predict register values (that's the unit DVs'
  job), it just confirms responses come back OK / DECERR as appropriate
  via per-test assertions in the vseq bodies.

The firmware_* tests drive AXI-Lite transactions directly through the
cocotbext-axi master via the firmware bridge (not through the dv_lib
sequencer), so those transactions don't go through the agent's driver
mirror and the scoreboard won't see them. That's deliberate: the
firmware tests check the firmware's return code, which encodes pass/fail
for every check the C did. The integration scoreboard's value-add is
the AXIS comparison, which is independent of the firmware path.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from dv import (
    AxiLiteAgent, AxiLiteAgentCfg, AxiLiteItem,
    AxiStreamAgent, AxiStreamAgentCfg, AxiStreamItem,
)


_log = logging.getLogger("dv_lib.plover")


class PloverScoreboard(DVBaseScoreboard):
    """Accumulates AXIS expected totals; counts AxiLite items for diagnostics."""

    def build_phase(self) -> None:
        super().build_phase()
        self.axil_fifo = uvm_tlm_analysis_fifo("axil_fifo", self)
        self.axis_fifo = uvm_tlm_analysis_fifo("axis_fifo", self)
        self.axil_count = 0
        # Running totals for the stream side, same model as stream_sink.
        self.exp_beats = 0
        self.exp_xor = 0
        self._data_mask = 0xFFFF_FFFF

    async def run_phase(self) -> None:
        # Two consumers; race them with start_soon.
        import cocotb
        cocotb.start_soon(self._consume_axil())
        cocotb.start_soon(self._consume_axis())

    async def _consume_axil(self) -> None:
        while True:
            _: AxiLiteItem = await self.axil_fifo.get()
            self.axil_count += 1

    async def _consume_axis(self) -> None:
        while True:
            item: AxiStreamItem = await self.axis_fifo.get()
            self.exp_beats = (self.exp_beats + 1) & 0xFFFF_FFFF
            self.exp_xor ^= (item.data & self._data_mask)


# ---- Env cfg + env --------------------------------------------------

class PloverEnvCfg(DVBaseEnvCfg):
    """vif is the cocotb DUT handle; reused by both agents."""

    def __init__(self, name: str = "plover_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.axil_agent_cfg: Optional[AxiLiteAgentCfg] = None
        self.axis_agent_cfg: Optional[AxiStreamAgentCfg] = None

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.axil_agent_cfg = AxiLiteAgentCfg("axil_agent_cfg")
        self.axil_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axil", self.axil_agent_cfg)

        self.axis_agent_cfg = AxiStreamAgentCfg("axis_agent_cfg")
        self.axis_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axis", self.axis_agent_cfg)


class PloverVirtualSequencer(DVBaseVirtualSequencer):
    pass


class PloverEnv(DVBaseEnv):
    cfg_type = PloverEnvCfg
    scoreboard_type = PloverScoreboard
    virtual_sequencer_type = PloverVirtualSequencer

    def __init__(self, name: str = "plover_env", parent=None) -> None:
        super().__init__(name, parent)
        self.axil_agent: Optional[AxiLiteAgent] = None
        self.axis_agent: Optional[AxiStreamAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: PloverEnvCfg = self.cfg  # type: ignore[assignment]
        assert cfg.axil_agent_cfg is not None
        assert cfg.axis_agent_cfg is not None
        cfg.axil_agent_cfg.vif = cfg.vif
        cfg.axis_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "axil_agent", "cfg", cfg.axil_agent_cfg)
        ConfigDB().set(self, "axis_agent", "cfg", cfg.axis_agent_cfg)
        self.axil_agent = AxiLiteAgent.create("axil_agent", self)
        self.axis_agent = AxiStreamAgent.create("axis_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        assert self.axil_agent is not None
        assert self.axis_agent is not None
        sb: PloverScoreboard = self.scoreboard  # type: ignore[assignment]
        self.axil_agent.monitor.analysis_port.connect(sb.axil_fifo.analysis_export)
        self.axis_agent.monitor.analysis_port.connect(sb.axis_fifo.analysis_export)
        if self.axil_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axil", self.axil_agent.sequencer)
        if self.axis_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axis", self.axis_agent.sequencer)
