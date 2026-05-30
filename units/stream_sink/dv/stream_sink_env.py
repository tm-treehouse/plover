"""Stream-sink env: scoreboard + Python reference model.

stream_sink is dead-simple — every accepted AXI-Stream beat increments
``beat_count`` and XORs ``tdata`` into ``data_xor``. No backpressure, no
register file, no state hiding. The "reference model" is therefore just two
running totals updated as the agent issues items.

The scoreboard collects the stimulus stream from the AxiStreamAgent's
monitor and accumulates the expected totals. At end-of-test, the test
samples the DUT's debug outputs and compares against these totals.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from dv import AxiStreamAgent, AxiStreamAgentCfg, AxiStreamItem


_log = logging.getLogger("dv_lib.stream_sink")


class StreamSinkRefModel:
    """Running totals updated per accepted AXI-Stream beat.

    The stream_sink RTL accepts every beat (no backpressure); the agent
    emits one item per beat; so the reference model just tallies whatever
    the agent sends. ``data_xor`` is masked to DATA_WIDTH bits.
    """

    def __init__(self, data_width: int = 32) -> None:
        self.data_width = data_width
        self.mask = (1 << data_width) - 1
        self.beat_count = 0
        self.data_xor = 0

    def consume(self, item: AxiStreamItem) -> None:
        self.beat_count = (self.beat_count + 1) & 0xFFFF_FFFF
        self.data_xor ^= (item.data & self.mask)


class StreamSinkScoreboard(DVBaseScoreboard):
    """Receives mirrored stream items from the agent and updates the model.

    Nothing to "score" cycle-by-cycle here: the DUT has no observable
    response on the AXI-Stream side (TREADY is statically asserted). The
    final beat_count / data_xor comparison happens at end-of-test via
    :meth:`expected_beats` / :meth:`expected_xor`.
    """

    def build_phase(self) -> None:
        super().build_phase()
        self.fifo = uvm_tlm_analysis_fifo("fifo", self)
        self.model: StreamSinkRefModel = StreamSinkRefModel()

    async def run_phase(self) -> None:
        while True:
            item: AxiStreamItem = await self.fifo.get()
            self.model.consume(item)

    # Public read-out for the test's end-of-run checks.
    @property
    def expected_beats(self) -> int:
        return self.model.beat_count

    @property
    def expected_xor(self) -> int:
        return self.model.data_xor


# ---- Env cfg + env --------------------------------------------------

class StreamSinkEnvCfg(DVBaseEnvCfg):
    """vif is the cocotb DUT handle; reused for the agent's vif too."""

    def __init__(self, name: str = "stream_sink_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.axis_agent_cfg: Optional[AxiStreamAgentCfg] = None

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.axis_agent_cfg = AxiStreamAgentCfg("axis_agent_cfg")
        self.axis_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axis", self.axis_agent_cfg)


class StreamSinkVirtualSequencer(DVBaseVirtualSequencer):
    """Exposes the AXIS sequencer under the name 'axis' to vseqs."""
    pass


class StreamSinkEnv(DVBaseEnv):
    cfg_type = StreamSinkEnvCfg
    scoreboard_type = StreamSinkScoreboard
    virtual_sequencer_type = StreamSinkVirtualSequencer

    def __init__(self, name: str = "stream_sink_env", parent=None) -> None:
        super().__init__(name, parent)
        self.axis_agent: Optional[AxiStreamAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: StreamSinkEnvCfg = self.cfg  # type: ignore[assignment]
        assert cfg.axis_agent_cfg is not None
        cfg.axis_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "axis_agent", "cfg", cfg.axis_agent_cfg)
        self.axis_agent = AxiStreamAgent.create("axis_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        assert self.axis_agent is not None
        assert self.scoreboard is not None
        sb: StreamSinkScoreboard = self.scoreboard  # type: ignore[assignment]
        self.axis_agent.monitor.analysis_port.connect(sb.fifo.analysis_export)
        if self.axis_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axis", self.axis_agent.sequencer)
