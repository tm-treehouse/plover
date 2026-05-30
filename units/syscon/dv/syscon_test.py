"""Syscon vseqs + base test."""
from __future__ import annotations

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from syscon_env import SysconEnv, SysconEnvCfg
from dv import AxiLiteItem, AxiLiteOp
import regmap as rm


class AxiLiteItemSeq(DVBaseSequence):
    def __init__(self, items, name: str = "syscon_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


class SysconSmokeVSeq(DVBaseVSeq):
    """Read every register; verify their initial software-visible values."""

    def __init__(self, name: str = "SysconSmokeVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["syscon"]
        items = [
            AxiLiteItem(op=AxiLiteOp.READ, addr=rm.VERSION.offset),
            AxiLiteItem(op=AxiLiteOp.READ, addr=rm.VERSION_HASH.offset),
            AxiLiteItem(op=AxiLiteOp.READ, addr=rm.SOFT_RST.offset),
            AxiLiteItem(op=AxiLiteOp.READ, addr=rm.RESET_CAUSE.offset),
            AxiLiteItem(op=AxiLiteOp.READ, addr=rm.FEATURES.offset),
        ]
        await AxiLiteItemSeq(items).start(seqr)


class SysconResetCauseVSeq(DVBaseVSeq):
    """Exercise the reset-cause latching + W1C semantics.

    Sequence:
      1. After POR, RESET_CAUSE.POR=1, .SOFT=0.
      2. Clear POR (W1C). Read confirms POR=0.
      3. Issue a soft-reset request via SOFT_RST.
      4. Read RESET_CAUSE: .SOFT should now be 1.
      5. Clear SOFT. Read confirms both bits cleared.
    """

    def __init__(self, name: str = "SysconResetCauseVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["syscon"]
        items = [
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.RESET_CAUSE.offset),   # 1
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=rm.RESET_CAUSE.offset, data=0x1),  # 2
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.RESET_CAUSE.offset),
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=rm.SOFT_RST.offset,    data=0x1),  # 3
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.RESET_CAUSE.offset),   # 4
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=rm.RESET_CAUSE.offset, data=0x2),  # 5
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.RESET_CAUSE.offset),
        ]
        await AxiLiteItemSeq(items).start(seqr)


class SysconBaseTest(DVBaseTest):
    """Owns the run-phase objection (pyuvm idiom). dv_lib base untouched."""
    cfg_type = SysconEnvCfg
    env_type = SysconEnv

    def __init__(self, name: str = "SysconBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "SysconSmokeVSeq"

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
        finally:
            self.drop_objection()


class SysconResetCauseTest(SysconBaseTest):
    def __init__(self, name: str = "SysconResetCauseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "SysconResetCauseVSeq"
