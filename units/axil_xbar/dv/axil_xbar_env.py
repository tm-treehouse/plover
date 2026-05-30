"""axil_xbar env: scoreboard + Python reference model.

The DUT is the xbar driving two RAM stubs at 0x0000_0000 and 0x0000_1000
(4 KB pages, configured by the DV harness). The reference model knows both
pieces:

* The xbar's decode (page bases + masks).
* The RAM stubs' behaviour (write-at-addr / read-returns-what-was-written,
  256 bytes per stub).

For each AXI-Lite item the agent emits, the model predicts what the
response should be — either ``RESP_OKAY`` with the expected data for
reads, or ``RESP_DECERR`` for unmapped addresses. The scoreboard receives
the mirrored items (already back-annotated by the driver with the actual
DUT response) and compares.
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


_log = logging.getLogger("dv_lib.axil_xbar")


# Same RESP codes as in the cocotbext-axi back-annotation.
RESP_OKAY   = 0
RESP_DECERR = 3


class AxilXbarRefModel:
    """Routing + RAM-stub state model.

    Address decode mirrors the xbar parameters: a transaction with address
    ``A`` is routed to slave ``i`` iff ``(A & SLAVE_MASK[i]) == SLAVE_BASE[i]``.
    Each modelled slave is a simple byte-addressable RAM at full 32-bit
    word granularity (matching the behaviour of ``axil_ram_stub.sv``).

    The harness's DV top instantiates two RAMs of MEM_BYTES bytes each at
    bases 0x0000_0000 and 0x0000_1000.
    """

    def __init__(self, slave_base, slave_mask, mem_bytes: int = 256,
                 data_width: int = 32) -> None:
        assert len(slave_base) == len(slave_mask)
        self.slave_base = list(slave_base)
        self.slave_mask = list(slave_mask)
        self.mem_bytes = mem_bytes
        self.data_width = data_width
        self.byte_lanes = data_width // 8
        self.mask = (1 << data_width) - 1
        self.n = len(slave_base)
        # Per-slave word RAM: word_index -> value.
        self.rams: list[dict[int, int]] = [{} for _ in range(self.n)]

    def _decode(self, addr: int) -> Optional[int]:
        """Return slave index, or None if unmapped."""
        for i in range(self.n):
            if (addr & self.slave_mask[i]) == self.slave_base[i]:
                return i
        return None

    def _word_index(self, addr: int) -> int:
        """Strip page base + byte offset bits to get the word index inside the RAM."""
        # axil_ram_stub uses addr[2 +: IDX_W] as the index, where IDX_W is
        # log2(MEM_BYTES / byte_lanes). We just shift right by 2 and mask.
        words = self.mem_bytes // self.byte_lanes
        return (addr // self.byte_lanes) % words

    def predict(self, item: AxiLiteItem) -> tuple[int, int]:
        """Return (expected_resp, expected_data_for_read).

        For writes, expected_data_for_read is unused (returned as 0).
        Side-effect: updates the per-slave RAM on a successful write.
        """
        slave = self._decode(item.addr)
        if slave is None:
            return RESP_DECERR, 0
        idx = self._word_index(item.addr)
        if item.op is AxiLiteOp.WRITE:
            self.rams[slave][idx] = item.data & self.mask
            return RESP_OKAY, 0
        else:  # READ
            value = self.rams[slave].get(idx, 0)
            return RESP_OKAY, value


class AxilXbarScoreboard(DVBaseScoreboard):
    """Compares each driven item against the prediction.

    The agent's driver back-annotates ``item.resp`` and (for reads)
    ``item.data`` with what the DUT actually returned. The model predicts
    what *should* have happened from the same item. Mismatches accumulate
    a count and log loudly; ``do_check`` raises at end-of-test if any
    occurred.
    """

    def build_phase(self) -> None:
        super().build_phase()
        self.fifo = uvm_tlm_analysis_fifo("fifo", self)
        # Default address map (same as the DV harness). The test can
        # rebuild ``model`` from env cfg if it wants different bases.
        self.model = AxilXbarRefModel(
            slave_base=[0x0000_0000, 0x0000_1000],
            slave_mask=[0xFFFF_F000, 0xFFFF_F000],
        )
        self.matches = 0
        self.mismatches = 0

    async def run_phase(self) -> None:
        while True:
            item: AxiLiteItem = await self.fifo.get()
            exp_resp, exp_data = self.model.predict(item)
            if item.op is AxiLiteOp.WRITE:
                if item.resp != exp_resp:
                    self._fail(
                        f"WRITE @0x{item.addr:08x} data=0x{item.data:08x}: "
                        f"got resp={item.resp}, expected {exp_resp}")
                else:
                    self.matches += 1
            else:
                if item.resp != exp_resp:
                    self._fail(
                        f"READ @0x{item.addr:08x}: "
                        f"got resp={item.resp}, expected {exp_resp}")
                elif item.resp == RESP_OKAY and item.data != exp_data:
                    self._fail(
                        f"READ @0x{item.addr:08x}: "
                        f"got data=0x{item.data:08x}, "
                        f"expected 0x{exp_data:08x}")
                else:
                    self.matches += 1

    def _fail(self, msg: str) -> None:
        self.mismatches += 1
        _log.error(msg)

    def do_check(self) -> None:
        if self.mismatches:
            _log.error(
                f"axil_xbar_scoreboard: {self.mismatches} mismatch(es), "
                f"{self.matches} match(es)")
            assert False, f"{self.mismatches} scoreboard mismatch(es)"
        else:
            _log.info(
                f"axil_xbar_scoreboard PASS: {self.matches} transaction(s) checked")


# ---- Env cfg + env --------------------------------------------------

class AxilXbarEnvCfg(DVBaseEnvCfg):
    """vif is the cocotb DUT handle; reused for the agent's vif too."""

    def __init__(self, name: str = "axil_xbar_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.axil_agent_cfg: Optional[AxiLiteAgentCfg] = None

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.axil_agent_cfg = AxiLiteAgentCfg("axil_agent_cfg")
        self.axil_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axil", self.axil_agent_cfg)


class AxilXbarVirtualSequencer(DVBaseVirtualSequencer):
    pass


class AxilXbarEnv(DVBaseEnv):
    cfg_type = AxilXbarEnvCfg
    scoreboard_type = AxilXbarScoreboard
    virtual_sequencer_type = AxilXbarVirtualSequencer

    def __init__(self, name: str = "axil_xbar_env", parent=None) -> None:
        super().__init__(name, parent)
        self.axil_agent: Optional[AxiLiteAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: AxilXbarEnvCfg = self.cfg  # type: ignore[assignment]
        assert cfg.axil_agent_cfg is not None
        cfg.axil_agent_cfg.vif = cfg.vif
        ConfigDB().set(self, "axil_agent", "cfg", cfg.axil_agent_cfg)
        self.axil_agent = AxiLiteAgent.create("axil_agent", self)

    def connect_phase(self) -> None:
        super().connect_phase()
        assert self.axil_agent is not None
        assert self.scoreboard is not None
        sb: AxilXbarScoreboard = self.scoreboard  # type: ignore[assignment]
        self.axil_agent.monitor.analysis_port.connect(sb.fifo.analysis_export)
        if self.axil_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axil", self.axil_agent.sequencer)
