"""axil_xbar unit test classes + virtual sequences.

Three vseqs map to the three testcases the old direct test had:

* smoke      — write to each page, read back, confirm cross-slave
               isolation. The scoreboard verifies every transaction
               against the routing + RAM model.
* decerr     — write/read to an unmapped address. Scoreboard expects
               RESP_DECERR. Mapped slaves still work after.
* concurrent — seed slave-1, then issue write-to-slave-0 + read-from-
               slave-1. The xbar's separate read/write FSMs let
               cocotbext-axi pipeline these freely.

``AxilXbarBaseTest`` owns the run-phase objection and the
scoreboard's end-of-test ``do_check`` call.
"""
from __future__ import annotations

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from dv import AxiLiteItem, AxiLiteOp

from axil_xbar_env import AxilXbarEnv, AxilXbarEnvCfg


SLAVE0_BASE = 0x0000_0000
SLAVE1_BASE = 0x0000_1000
UNMAPPED    = 0x0000_2000


# ---- Item sub-sequence ----------------------------------------------

class AxilXbarItemSeq(DVBaseSequence):
    """Issue a pre-built list of AxiLiteItems on the axil sequencer."""

    def __init__(self, items, name: str = "axil_xbar_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Virtual sequences ----------------------------------------------

class AxilXbarSmokeVSeq(DVBaseVSeq):
    """Write to each page, read back, probe cross-slave isolation."""

    def __init__(self, name: str = "AxilXbarSmokeVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=SLAVE0_BASE + 0x04, data=0xCAFE_F00D),
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=SLAVE1_BASE + 0x08, data=0xDEAD_BEEF),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE0_BASE + 0x04),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE1_BASE + 0x08),
            # Cross-slave isolation probes: slave-0 at slave-1's offset
            # should still read 0 (the RAMs are independent).
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE0_BASE + 0x08),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE1_BASE + 0x04),
        ]
        await AxilXbarItemSeq(items).start(seqr)


class AxilXbarDecerrVSeq(DVBaseVSeq):
    """Unmapped address returns DECERR; mapped slaves still work after."""

    def __init__(self, name: str = "AxilXbarDecerrVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=UNMAPPED, data=0x1234_5678),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=UNMAPPED),
            # After the DECERR, mapped slaves must still respond.
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=SLAVE0_BASE + 0x0C, data=0xABCD),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE0_BASE + 0x0C),
        ]
        await AxilXbarItemSeq(items).start(seqr)


class AxilXbarConcurrentVSeq(DVBaseVSeq):
    """Seed slave-1, then write-to-slave-0 + read-from-slave-1 back-to-back.

    The xbar's separate read/write FSMs let cocotbext-axi pipeline these
    freely; the scoreboard verifies both responses and the read value.
    """

    def __init__(self, name: str = "AxilXbarConcurrentVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=SLAVE1_BASE + 0x10, data=0xBEEF_F00D),
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=SLAVE0_BASE + 0x00, data=0x99),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=SLAVE1_BASE + 0x10),
        ]
        await AxilXbarItemSeq(items).start(seqr)


# ---- Base test -------------------------------------------------------

class AxilXbarBaseTest(DVBaseTest):
    """Owns the run-phase objection and the scoreboard's end-of-test check."""
    cfg_type = AxilXbarEnvCfg
    env_type = AxilXbarEnv

    def __init__(self, name: str = "AxilXbarBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilXbarSmokeVSeq"

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
            # Final tally / raise on any mismatch.
            self.env.scoreboard.do_check()  # type: ignore[union-attr]
        finally:
            self.drop_objection()


class AxilXbarDecerrTest(AxilXbarBaseTest):
    def __init__(self, name: str = "AxilXbarDecerrTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilXbarDecerrVSeq"


class AxilXbarConcurrentTest(AxilXbarBaseTest):
    def __init__(self, name: str = "AxilXbarConcurrentTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilXbarConcurrentVSeq"
