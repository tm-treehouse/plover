"""
AXI-Lite shell environment, built on the dv_lib base classes.

Structure mirrors the dv_lib ALU example's env, scaled down to the single
AXI-Lite endpoint:

* :class:`AxilEnvCfg` carries the cocotb DUT handle (``vif``) and one agent
  cfg. ``initialize`` constructs the agent cfg so the test can tweak it.
* :class:`RegModel` is an independent Python golden model of the shell's
  register map; :class:`AxilScoreboard` applies every observed transaction to
  it and checks reads against it.
* :class:`AxilVirtualSequencer` registers the agent's sequencer so vseqs can
  drive stimulus.
* :class:`AxilEnv` instantiates the agent, propagates its cfg, and connects
  the monitor to the scoreboard FIFO.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from .axil_agent import AxilAgent, AxilAgentCfg, AxilItem, AxilOp


_log = logging.getLogger("dv_lib.axil_shell")


# ---- Reference model -------------------------------------------------

from . import regmap as rm


class RegModel:
    """Golden model of the register map, driven by the generated RDL map.

    Addresses, field masks, reset values, and access types all come from
    ``regmap`` (generated from ``rdl/axil_shell.rdl``), so editing the RDL and
    regenerating updates this model with no hand-changes. The only behavior
    encoded here that the static map can't express is the STATUS.READY ->
    CONTROL.ENABLE hardware mirror, which is design logic, not map data.
    """

    MASK = 0xFFFF_FFFF

    def __init__(self) -> None:
        # Software-writable registers get mutable storage seeded from reset.
        self.storage: dict[str, int] = {}
        for name, reg in rm.REGISTERS.items():
            reset = 0
            for f in reg.fields.values():
                reset |= (f.reset << f.lsb) & f.mask
            self.storage[name] = reset

    def _reg_at(self, addr: int):
        a = addr & 0xFC
        for reg in rm.REGISTERS.values():
            if reg.offset == a:
                return reg
        return None

    def apply_write(self, addr: int, data: int) -> None:
        reg = self._reg_at(addr)
        if reg is None:
            return
        # Honor per-field sw-writability: only writable field bits update.
        wmask = 0
        for f in reg.fields.values():
            if f.sw_writable:
                wmask |= f.mask
        if wmask:
            cur = self.storage[reg.name]
            self.storage[reg.name] = (cur & ~wmask) | (data & wmask & self.MASK)

    def expected_read(self, addr: int) -> int:
        reg = self._reg_at(addr)
        if reg is None:
            return 0
        # STATUS.READY is a hardware mirror of CONTROL.ENABLE (design logic).
        if reg.name == "STATUS":
            enable = self.storage["CONTROL"] & rm.CONTROL.field("ENABLE").mask
            return enable & rm.STATUS.field("READY").mask
        return self.storage[reg.name] & self.MASK


# ---- Env cfg ---------------------------------------------------------

class AxilEnvCfg(DVBaseEnvCfg):
    def __init__(self, name: str = "axil_env_cfg") -> None:
        super().__init__(name)
        self.vif = None                                   # cocotb DUT handle
        self.axil_agent_cfg: Optional[AxilAgentCfg] = None

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.axil_agent_cfg = AxilAgentCfg("axil_agent_cfg")
        self.axil_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axil", self.axil_agent_cfg)


# ---- Scoreboard ------------------------------------------------------

class AxilScoreboard(DVBaseScoreboard):
    """Subscribes to the agent's analysis port, models the register file,
    and checks every read against the model. Gated on ``cfg.en_scb``.
    """

    def __init__(self, name: str = "axil_scoreboard", parent=None) -> None:
        super().__init__(name, parent)
        self.axil_fifo: Optional[uvm_tlm_analysis_fifo] = None
        self.model = RegModel()
        self.matches = 0
        self.mismatches = 0

    def build_phase(self) -> None:
        super().build_phase()
        self.axil_fifo = self.make_fifo("axil_fifo")

    async def run_phase(self) -> None:
        await super().run_phase()
        if self.cfg is not None and not self.cfg.en_scb:
            return
        assert self.axil_fifo is not None
        while True:
            item: AxilItem = await self.axil_fifo.get()
            self._check(item)

    def _check(self, item: AxilItem) -> None:
        if item.op is AxilOp.WRITE:
            self.model.apply_write(item.addr, item.data)
            return
        expected = self.model.expected_read(item.addr)
        if item.data == expected:
            self.matches += 1
        else:
            self.mismatches += 1
            _log.error(
                f"READ MISMATCH @0x{item.addr:08x}: "
                f"got 0x{item.data:08x}, expected 0x{expected:08x}")

    def do_check(self) -> None:
        if self.mismatches:
            _log.error(
                f"axil_scoreboard: {self.mismatches} mismatch(es), "
                f"{self.matches} match(es)")
            assert False, f"{self.mismatches} scoreboard mismatch(es)"
        else:
            _log.info(f"axil_scoreboard PASS: {self.matches} read(s) checked")


# ---- Virtual sequencer ----------------------------------------------

class AxilVirtualSequencer(DVBaseVirtualSequencer):
    pass


# ---- Env -------------------------------------------------------------

class AxilEnv(DVBaseEnv):
    cfg_type = AxilEnvCfg
    scoreboard_type = AxilScoreboard
    virtual_sequencer_type = AxilVirtualSequencer

    def __init__(self, name: str = "axil_env", parent=None) -> None:
        super().__init__(name, parent)
        self.axil_agent: Optional[AxilAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: AxilEnvCfg = self.cfg  # type: ignore[assignment]
        # Wire the agent cfg's vif to the env's DUT handle.
        cfg.axil_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "axil_agent", "cfg", cfg.axil_agent_cfg)
        self.axil_agent = AxilAgent.create("axil_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        sb: AxilScoreboard = self.scoreboard  # type: ignore[assignment]
        self.axil_agent.monitor.analysis_port.connect(
            sb.axil_fifo.analysis_export)
        if self.axil_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr(
                "axil", self.axil_agent.sequencer)
