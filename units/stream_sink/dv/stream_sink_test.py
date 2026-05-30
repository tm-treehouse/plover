"""Stream-sink unit test classes and virtual sequence.

The vseq pushes a fixed pattern of AXI-Stream beats; the scoreboard
(in stream_sink_env.py) accumulates the expected beat_count and data_xor
as the agent emits items. ``StreamSinkBaseTest`` owns the run-phase
objection and samples the DUT's debug outputs at end-of-run to compare
against the scoreboard's expected values.

Why the final check lives in the test class rather than the scoreboard:
the stream_sink RTL has no per-beat observation channel — beat_count and
data_xor are continuous outputs that only matter once at the end. Putting
the sampling logic in the test body keeps the scoreboard purely
transactional.
"""
from __future__ import annotations

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

import cocotb
from cocotb.triggers import ClockCycles

from dv import AxiStreamItem

from stream_sink_env import StreamSinkEnv, StreamSinkEnvCfg


# ---- Item sub-sequence ----------------------------------------------

class StreamSinkItemSeq(DVBaseSequence):
    """Issue a pre-built list of AxiStreamItems on the AXIS sequencer."""

    def __init__(self, items, name: str = "stream_sink_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Virtual sequence -----------------------------------------------

class StreamSinkSmokeVSeq(DVBaseVSeq):
    """Push a fixed pattern of beats. TLAST is asserted on the last beat."""

    PATTERN = [
        0x00000001, 0x00000002, 0xDEADBEEF, 0x12345678,
        0xA5A5A5A5, 0x5A5A5A5A,
    ]

    def __init__(self, name: str = "StreamSinkSmokeVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axis"]
        items = [AxiStreamItem(data=w) for w in self.PATTERN]
        await StreamSinkItemSeq(items).start(seqr)


# ---- Base test -------------------------------------------------------

class StreamSinkBaseTest(DVBaseTest):
    """Owns the run-phase objection AND the end-of-run DUT sampling.

    After the vseq's body drains, the scoreboard's :attr:`expected_beats`
    and :attr:`expected_xor` hold what the DUT *should* show. We sample
    the live DUT signals (with a small settle delay) and compare.
    """
    cfg_type = StreamSinkEnvCfg
    env_type = StreamSinkEnv

    def __init__(self, name: str = "StreamSinkBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "StreamSinkSmokeVSeq"
        # Cycles to wait after the last AXIS beat before sampling beat_count
        # and data_xor (lets the last beat commit into the registers).
        self.settle_cycles = 3

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
            await self._check_dut()
        finally:
            self.drop_objection()

    async def _check_dut(self) -> None:
        """Sample the DUT's beat_count / data_xor and compare to the model."""
        if not cocotb.is_simulation:
            return
        dut = cocotb.top
        # Give the sink a couple of cycles for the last beat to commit
        # before sampling.
        await ClockCycles(dut.clk, self.settle_cycles)
        beat_count = int(dut.beat_count.value)
        data_xor   = int(dut.data_xor.value)

        sb = self.env.scoreboard  # type: ignore[union-attr]
        exp_beats = sb.expected_beats
        exp_xor   = sb.expected_xor

        self.logger.info(
            f"stream_sink final state: beat_count={beat_count} "
            f"data_xor=0x{data_xor:08x} (expected beats={exp_beats}, "
            f"xor=0x{exp_xor:08x})")
        assert beat_count == exp_beats, (
            f"beat_count mismatch: got {beat_count}, expected {exp_beats}")
        assert data_xor == exp_xor, (
            f"data_xor mismatch: got 0x{data_xor:08x}, "
            f"expected 0x{exp_xor:08x}")
