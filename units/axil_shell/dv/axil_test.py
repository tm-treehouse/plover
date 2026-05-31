"""
AXI-Lite shell virtual sequences + base test.

The vseqs run on the env's virtual sequencer and drive AXI-Lite item
sub-sequences on the agent's sequencer (registered under "axil"). Reset is
driven through the env cfg's ``clk_rst_vif`` by the dv_lib base ``apply_reset``.

Tests are selected the dv_lib / OpenTitan way: ``DVBaseTest`` reads the
``UVM_TEST_SEQ`` plusarg (or the ``test_seq_s`` default set in __init__) and
runs that vseq by name.
"""
from __future__ import annotations

import random

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from axil_env import AxilEnv, AxilEnvCfg
from dv import AxiLiteItem, AxiLiteOp
import regmap as rm


# ---- An agent-level item sub-sequence -------------------------------

class AxiLiteItemSeq(DVBaseSequence):
    """Runs a pre-built list of AxiLiteItems on the AXI-Lite sequencer."""

    def __init__(self, items, name: str = "axil_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Virtual sequences ----------------------------------------------

class AxilSmokeVSeq(DVBaseVSeq):
    """Write SCRATCH, read it back, then read the constant ID register."""

    def __init__(self, name: str = "AxilSmokeVSeq") -> None:
        super().__init__(name)
        self.reset_cycles = 5

    async def body(self) -> None:
        await super().body()  # dv_init -> apply_reset via clk_rst_vif
        seqr = self.p_sequencer.sub_seqrs["axil"]
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=rm.SCRATCH.offset, data=0xA5A5_1234),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.SCRATCH.offset),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=rm.ID.offset),
        ]
        await AxiLiteItemSeq(items).start(seqr)


class AxilSweepVSeq(DVBaseVSeq):
    """Randomized write/read sweep over the R/W register space."""

    def __init__(self, name: str = "AxilSweepVSeq") -> None:
        super().__init__(name)
        self.num_txns = 64
        # Software-writable registers, taken from the generated map.
        self.rw_addrs = tuple(
            reg.offset for reg in rm.REGISTERS.values()
            if any(f.sw_writable for f in reg.fields.values())
        )
        self.seed = 1

    async def body(self) -> None:
        await super().body()
        rng = random.Random(self.seed)
        seqr = self.p_sequencer.sub_seqrs["axil"]
        items = []
        for _ in range(self.num_txns):
            addr = rng.choice(self.rw_addrs)
            if rng.random() < 0.5:
                items.append(AxiLiteItem(op=AxiLiteOp.WRITE, addr=addr,
                                      data=rng.getrandbits(32)))
            else:
                items.append(AxiLiteItem(op=AxiLiteOp.READ, addr=addr))
        await AxiLiteItemSeq(items).start(seqr)


class AxilControlPortsVSeq(DVBaseVSeq):
    """Drive the CONTROL register and verify the new field-output ports
    (``control_enable``, ``control_spare``) reflect the written bits.

    The ports are combinational fan-out from ``reg_control``, so they
    update on the cycle the write commits. We issue each write through
    the sequencer, wait one cycle for the assignment to settle on the
    output, then sample.
    """

    def __init__(self, name: str = "AxilControlPortsVSeq") -> None:
        super().__init__(name)

    async def _write_and_check(self, seqr, value: int) -> None:
        await AxiLiteItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=rm.CONTROL.offset, data=value),
        ]).start(seqr)
        # One extra cycle for the continuous assign to propagate after B.
        import cocotb
        from cocotb.triggers import ClockCycles
        await ClockCycles(cocotb.top.clk, 1)
        got_enable = int(cocotb.top.control_enable.value)
        got_spare  = int(cocotb.top.control_spare.value)
        exp_enable = value & 0x1
        exp_spare  = (value >> 1) & 0x7FFF_FFFF
        assert got_enable == exp_enable, (
            f"control_enable port: wrote CONTROL=0x{value:08x}, "
            f"expected enable={exp_enable}, got {got_enable}")
        assert got_spare == exp_spare, (
            f"control_spare port: wrote CONTROL=0x{value:08x}, "
            f"expected spare=0x{exp_spare:08x}, got 0x{got_spare:08x}")

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        # Sweep ENABLE on/off and exercise a few spare-bit patterns.
        await self._write_and_check(seqr, 0x0000_0001)  # enable=1, spare=0
        await self._write_and_check(seqr, 0x0000_0000)  # enable=0, spare=0
        await self._write_and_check(seqr, 0xAAAA_AAAB)  # mixed pattern, enable=1
        await self._write_and_check(seqr, 0x5555_5554)  # mixed pattern, enable=0


# ---- Base test -------------------------------------------------------

class AxilBaseTest(DVBaseTest):
    """SV equivalent: ``axil_base_test extends dv_base_test
    #(.CFG_T(axil_env_cfg), .ENV_T(axil_env));``

    ``DVBaseTest.run_phase`` is a faithful port of the SystemVerilog
    ``dv_base_test`` where phase objections are implicit (the SV phaser
    raises/drops them around the test). pyuvm's phaser does not do that
    implicitly — its ``ObjectionHandler`` keeps the run phase alive only while
    some component holds an objection — so the standard pyuvm idiom is for the
    test's ``run_phase`` to raise the objection while the sequence runs and
    drop it afterwards. We do that here, in our own leaf test, leaving the
    dv_lib base untouched.
    """
    cfg_type = AxilEnvCfg
    env_type = AxilEnv

    def __init__(self, name: str = "AxilBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilSmokeVSeq"  # default; override via UVM_TEST_SEQ

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
        finally:
            self.drop_objection()


class AxilSweepTest(AxilBaseTest):
    def __init__(self, name: str = "AxilSweepTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilSweepVSeq"


class AxilControlPortsTest(AxilBaseTest):
    def __init__(self, name: str = "AxilControlPortsTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "AxilControlPortsVSeq"
