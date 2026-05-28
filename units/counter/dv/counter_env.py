"""
Counter unit env: env cfg, scoreboard, virtual sequencer.

The scoreboard maintains a 1-line Python golden model of the counter and
compares each monitor observation against it. When you adapt this template
to a real sub-unit, the golden model is the part you replace with whatever
"the correct behaviour is" looks like in Python.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from counter_agent import CounterAgent, CounterAgentCfg, CounterItem


_log = logging.getLogger("dv_lib.counter")


# ---- Reference model ------------------------------------------------

class RefModel:
    """Golden model of counter.sv.

    Apply stimulus first (``step``), then read ``value`` — this mirrors the
    RTL's "register the input, present the new value on the next cycle"
    behaviour, and is what the monitor sees one edge later.
    """

    def __init__(self, width: int = 8) -> None:
        self.mask = (1 << width) - 1
        self.value = 0

    def step(self, clear: bool, enable: bool) -> None:
        if clear:
            self.value = 0
        elif enable:
            self.value = (self.value + 1) & self.mask


# ---- Env cfg --------------------------------------------------------

class CounterEnvCfg(DVBaseEnvCfg):
    def __init__(self, name: str = "counter_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.counter_agent_cfg: Optional[CounterAgentCfg] = None
        self.width: int = 8

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.counter_agent_cfg = CounterAgentCfg("counter_agent_cfg")
        self.counter_agent_cfg.is_active = UVM_ACTIVE
        self.counter_agent_cfg.width = self.width
        self.add_agent_cfg("counter", self.counter_agent_cfg)


# ---- Scoreboard -----------------------------------------------------

class CounterScoreboard(DVBaseScoreboard):
    """Subscribes to the monitor, advances the golden model in lockstep with
    the items the driver issued, and compares observations to the model.
    """

    def __init__(self, name: str = "counter_scoreboard", parent=None) -> None:
        super().__init__(name, parent)
        self.obs_fifo: Optional[uvm_tlm_analysis_fifo] = None
        self.stim_fifo: Optional[uvm_tlm_analysis_fifo] = None
        self.model = RefModel()
        self.matches = 0
        self.mismatches = 0

    def build_phase(self) -> None:
        super().build_phase()
        self.obs_fifo = self.make_fifo("obs_fifo")
        self.stim_fifo = self.make_fifo("stim_fifo")

    async def run_phase(self) -> None:
        await super().run_phase()
        if self.cfg is not None and not self.cfg.en_scb:
            return
        assert self.obs_fifo is not None and self.stim_fifo is not None
        while True:
            # Pair each driven item with the observation that follows it.
            item: CounterItem = await self.stim_fifo.get()
            self.model.step(item.clear, item.enable)
            obs = await self.obs_fifo.get()
            self._check(obs.count, self.model.value, cycle=obs.cycle)

    def _check(self, got: int, exp: int, *, cycle: int) -> None:
        if got == exp:
            self.matches += 1
        else:
            self.mismatches += 1
            _log.error(
                f"COUNT MISMATCH @cycle={cycle}: got 0x{got:x}, expected 0x{exp:x}")

    def do_check(self) -> None:
        if self.mismatches:
            _log.error(
                f"counter_scoreboard: {self.mismatches} mismatch(es), "
                f"{self.matches} match(es)")
            assert False, f"{self.mismatches} scoreboard mismatch(es)"
        else:
            _log.info(f"counter_scoreboard PASS: {self.matches} cycle(s) checked")


# ---- Virtual sequencer ---------------------------------------------

class CounterVirtualSequencer(DVBaseVirtualSequencer):
    pass


# ---- Env ------------------------------------------------------------

class CounterEnv(DVBaseEnv):
    cfg_type = CounterEnvCfg
    scoreboard_type = CounterScoreboard
    virtual_sequencer_type = CounterVirtualSequencer

    def __init__(self, name: str = "counter_env", parent=None) -> None:
        super().__init__(name, parent)
        self.counter_agent: Optional[CounterAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: CounterEnvCfg = self.cfg  # type: ignore[assignment]
        cfg.counter_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "counter_agent", "cfg", cfg.counter_agent_cfg)
        self.counter_agent = CounterAgent.create("counter_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        sb: CounterScoreboard = self.scoreboard  # type: ignore[assignment]
        # Monitor observations -> scoreboard obs side.
        self.counter_agent.monitor.analysis_port.connect(sb.obs_fifo.analysis_export)
        # Driver stimulus mirror -> scoreboard stim side (paired with each obs).
        self.counter_agent.driver.stim_ap.connect(sb.stim_fifo.analysis_export)
        if self.counter_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("counter", self.counter_agent.sequencer)
