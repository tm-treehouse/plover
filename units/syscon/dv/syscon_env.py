"""Syscon env: scoreboard + Python reference model.

The reference model encodes the non-trivial software-visible semantics of
this block, which the generated regmap can't fully express by itself:

* VERSION / VERSION_HASH: read-only, value driven by the test (either the
  parameter override the RTL was built with, or the build-time header).
* SOFT_RST.CORE: singlepulse. Writes drive the soft-reset pulse output;
  software reads always return 0.
* RESET_CAUSE.POR / SOFT: woclr. POR auto-sets after reset. SOFT sets when
  a soft-reset request fires. Each clears individually on write-1.
* FEATURES: constant.
"""
from __future__ import annotations

import logging
from typing import Optional

from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from dv import AxiLiteAgent, AxiLiteAgentCfg, AxiLiteItem, AxiLiteOp
import regmap as rm


_log = logging.getLogger("dv_lib.syscon")


class RegModel:
    """Golden model of syscon.sv.

    ``version_value`` and ``version_hash`` are expectations the test supplies
    (they depend on the parameters the RTL was built with). Everything else
    is derived from the generated regmap.
    """

    MASK = 0xFFFF_FFFF

    def __init__(self, version_value: int, version_hash: int) -> None:
        self.version_value = version_value & self.MASK
        self.version_hash = version_hash & self.MASK
        # RESET_CAUSE bits — POR auto-set on entry to run.
        self.cause_por = 1
        self.cause_soft = 0
        # FEATURES bit 0 is wired to 1 in the RTL (HAS_COUNTER).
        self.features = 1
        # Tracks soft-reset pulses observed (test assertion).
        self.soft_rst_pulses = 0

    # ---- Per-address logic --------------------------------------------

    def _reg_at(self, addr: int):
        a = addr & 0x1C  # registers are at 0x00, 0x04, 0x08, 0x0C, 0x10
        for reg in rm.REGISTERS.values():
            if reg.offset == a:
                return reg
        return None

    def apply_write(self, addr: int, data: int) -> None:
        reg = self._reg_at(addr)
        if reg is None:
            return
        data &= self.MASK
        if reg.name == "SOFT_RST":
            if data & 0x1:
                self.soft_rst_pulses += 1
                # SOFT cause bit latches on the pulse, matching the RTL.
                self.cause_soft = 1
        elif reg.name == "RESET_CAUSE":
            if data & 0x1:
                self.cause_por = 0
            if data & 0x2:
                self.cause_soft = 0
        # VERSION / VERSION_HASH / FEATURES: writes dropped (read-only).

    def expected_read(self, addr: int) -> int:
        reg = self._reg_at(addr)
        if reg is None:
            return 0
        if reg.name == "VERSION":
            return self.version_value
        if reg.name == "VERSION_HASH":
            return self.version_hash
        if reg.name == "SOFT_RST":
            return 0
        if reg.name == "RESET_CAUSE":
            return (self.cause_soft << 1) | self.cause_por
        if reg.name == "FEATURES":
            return self.features
        return 0


class SysconEnvCfg(DVBaseEnvCfg):
    def __init__(self, name: str = "syscon_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.syscon_agent_cfg: Optional[AxiLiteAgentCfg] = None
        # Test-supplied expectations for the version registers.
        self.version_value: int = 0
        self.version_hash: int = 0

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.syscon_agent_cfg = AxiLiteAgentCfg("syscon_agent_cfg")
        self.syscon_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("syscon", self.syscon_agent_cfg)


class SysconScoreboard(DVBaseScoreboard):
    def __init__(self, name: str = "syscon_scoreboard", parent=None) -> None:
        super().__init__(name, parent)
        self.fifo: Optional[uvm_tlm_analysis_fifo] = None
        self.model: Optional[RegModel] = None
        self.matches = 0
        self.mismatches = 0

    def build_phase(self) -> None:
        super().build_phase()
        self.fifo = self.make_fifo("fifo")

    async def run_phase(self) -> None:
        await super().run_phase()
        if self.cfg is not None and not self.cfg.en_scb:
            return
        # cfg here is SysconEnvCfg; pull the expected version values from it.
        env_cfg: SysconEnvCfg = self.cfg  # type: ignore[assignment]
        self.model = RegModel(env_cfg.version_value, env_cfg.version_hash)
        assert self.fifo is not None
        while True:
            item: AxiLiteItem = await self.fifo.get()
            self._check(item)

    def _check(self, item: AxiLiteItem) -> None:
        assert self.model is not None
        if item.op is AxiLiteOp.WRITE:
            self.model.apply_write(item.addr, item.data)
            return
        expected = self.model.expected_read(item.addr)
        if item.data == expected:
            self.matches += 1
        else:
            self.mismatches += 1
            _log.error(
                f"READ MISMATCH @0x{item.addr:08x}: got 0x{item.data:08x}, "
                f"expected 0x{expected:08x}")

    def do_check(self) -> None:
        if self.mismatches:
            _log.error(
                f"syscon_scoreboard: {self.mismatches} mismatch(es), "
                f"{self.matches} match(es)")
            assert False, f"{self.mismatches} scoreboard mismatch(es)"
        else:
            _log.info(f"syscon_scoreboard PASS: {self.matches} read(s) checked")


class SysconVirtualSequencer(DVBaseVirtualSequencer):
    pass


class SysconEnv(DVBaseEnv):
    cfg_type = SysconEnvCfg
    scoreboard_type = SysconScoreboard
    virtual_sequencer_type = SysconVirtualSequencer

    def __init__(self, name: str = "syscon_env", parent=None) -> None:
        super().__init__(name, parent)
        self.syscon_agent: Optional[AxiLiteAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: SysconEnvCfg = self.cfg  # type: ignore[assignment]
        cfg.syscon_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "syscon_agent", "cfg", cfg.syscon_agent_cfg)
        self.syscon_agent = AxiLiteAgent.create("syscon_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        sb: SysconScoreboard = self.scoreboard  # type: ignore[assignment]
        self.syscon_agent.monitor.analysis_port.connect(sb.fifo.analysis_export)
        if self.syscon_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("syscon", self.syscon_agent.sequencer)
