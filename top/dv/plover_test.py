"""Plover top test classes + virtual sequences.

Three vseqs map to the three integration tests the direct testbench had:

* smoke               — both pages reachable via the xbar, DECERR for
                        unmapped, counter wired, soft-reset gates counter.
                        All-AxiLite-via-sequencer. Pure dv_lib flow.
* firmware_smoke      — bring up the AxiLiteMaster via the agent, hand
                        it to the firmware bridge, call C entry, expect
                        rc=0. AxiLite stays out of the sequencer for the
                        firmware-driven leg.
* firmware_concurrent — same as firmware_smoke but with AXIS stimulus
                        pushed concurrently via a sub-sequence on the
                        axis sequencer. The end-of-run beat_count /
                        data_xor comparison lives in the base test.

``PloverBaseTest`` owns the run-phase objection. End-of-run AXIS sampling
fires only for tests that pushed AXIS items (``self.expect_axis_check``),
mirroring how stream_sink's base test samples at end-of-run.
"""
from __future__ import annotations

import os
from pathlib import Path

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from dv import AxiLiteItem, AxiLiteOp, AxiStreamItem

from plover_env import PloverEnv, PloverEnvCfg


# ---- Address map (matches plover.sv xbar config) -------------------

SHELL_BASE  = 0x0000_0000
SYSCON_BASE = 0x0000_1000
UNMAPPED    = 0x0000_2000

SHELL_ID_OFFSET   = 0x0C
SHELL_ID_EXPECTED = 0xC0C07B01
SYSCON_VERSION_OFFSET   = 0x00
SYSCON_SOFT_RST_OFFSET  = 0x08
EXPECTED_SYSCON_VERSION = 0xCAFE_F00D
SYSCON_SOFT_RST_CYCLES  = 8

RESP_OKAY   = 0
RESP_DECERR = 3


# ---- Item sub-sequences ---------------------------------------------

class PloverAxilItemSeq(DVBaseSequence):
    """Issue a pre-built list of AxiLiteItems on the axil sequencer."""

    def __init__(self, items, name: str = "plover_axil_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


class PloverAxisItemSeq(DVBaseSequence):
    """Issue a pre-built list of AxiStreamItems on the axis sequencer."""

    def __init__(self, items, name: str = "plover_axis_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Virtual sequences ----------------------------------------------

class PloverSmokeVSeq(DVBaseVSeq):
    """Integration smoke: read both pages, exercise DECERR, soft-reset gate.

    Uses a mix of per-item AXI-Lite operations on the sequencer, with
    timing pauses (counter sampling, soft-reset window) done directly on
    the DUT clock. The vseq body owns those timing assertions because
    they're integration-level checks, not protocol-level — the scoreboard
    can't predict them.
    """

    def __init__(self, name: str = "PloverSmokeVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        dut = cocotb.top

        # 1) Read shell.ID and syscon.VERSION; values are known constants.
        items = [
            AxiLiteItem(op=AxiLiteOp.READ, addr=SHELL_BASE + SHELL_ID_OFFSET),
        ]
        await PloverAxilItemSeq(items).start(seqr)
        assert items[0].resp == RESP_OKAY, f"shell read resp {items[0].resp}"
        assert items[0].data == SHELL_ID_EXPECTED, (
            f"axil_shell.ID via xbar: got 0x{items[0].data:08x}, "
            f"expected 0x{SHELL_ID_EXPECTED:08x}")

        items = [
            AxiLiteItem(op=AxiLiteOp.READ, addr=SYSCON_BASE + SYSCON_VERSION_OFFSET),
        ]
        await PloverAxilItemSeq(items).start(seqr)
        assert items[0].resp == RESP_OKAY, f"syscon read resp {items[0].resp}"
        assert items[0].data == EXPECTED_SYSCON_VERSION, (
            f"syscon.VERSION via xbar: got 0x{items[0].data:08x}, "
            f"expected 0x{EXPECTED_SYSCON_VERSION:08x}")

        # 2) Unmapped address returns DECERR for both write and read.
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=UNMAPPED, data=0xDEADBEEF),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=UNMAPPED),
        ]
        await PloverAxilItemSeq(items).start(seqr)
        for i, exp_op in enumerate(("write", "read")):
            assert items[i].resp == RESP_DECERR, (
                f"{exp_op} to unmapped 0x{UNMAPPED:08x}: "
                f"got resp {items[i].resp}, expected DECERR (3)")

        # Mapped pages still work after the DECERR.
        items = [AxiLiteItem(op=AxiLiteOp.READ, addr=SHELL_BASE + SHELL_ID_OFFSET)]
        await PloverAxilItemSeq(items).start(seqr)
        assert items[0].resp == RESP_OKAY, "shell broken after DECERR"

        # 3) Counter is wired and advancing.
        await RisingEdge(dut.clk)
        start = int(dut.count.value)
        await ClockCycles(dut.clk, 10)
        end = int(dut.count.value)
        mask = (1 << len(dut.count)) - 1
        advanced = (end - start) & mask
        assert advanced == 10, (
            f"counter advance: expected 10, got {advanced} "
            f"(start=0x{start:x} end=0x{end:x})")

        # 4) Soft-reset via syscon gates the counter.
        await PloverAxilItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE,
                        addr=SYSCON_BASE + SYSCON_SOFT_RST_OFFSET,
                        data=1),
        ]).start(seqr)
        await ClockCycles(dut.clk, 3)
        mid = int(dut.count.value)
        assert mid == 0, (
            f"soft-reset gating: expected counter held at 0 mid-window, "
            f"got 0x{mid:x}")
        await ClockCycles(dut.clk, SYSCON_SOFT_RST_CYCLES + 2)
        after = int(dut.count.value)
        assert 1 <= after <= SYSCON_SOFT_RST_CYCLES + 4, (
            f"soft-reset release: counter small after window, got 0x{after:x}")


class PloverFirmwareSmokeVSeq(DVBaseVSeq):
    """Call the C firmware via the bridge. AxiLite goes outside the sequencer
    because the bridge's @resume callbacks need the raw AxiLiteMaster.
    """

    def __init__(self, name: str = "PloverFirmwareSmokeVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        from firmware_bridge import run_hello_world
        master = _master_from_env(self)
        include_dirs = _include_dirs_from_env()
        rc = await run_hello_world(
            master,
            shell_base=SHELL_BASE,
            syscon_base=SYSCON_BASE,
            expected_syscon_version=EXPECTED_SYSCON_VERSION,
            include_dirs=include_dirs,
        )
        assert rc == 0, f"plover_hello_world returned {rc} (non-zero = check failed)"


class PloverFirmwareConcurrentVSeq(DVBaseVSeq):
    """Push AXIS pattern in the background while the C firmware runs."""

    PATTERN = [
        0x00000001, 0x00000002, 0x00000004, 0x00000008,
        0xDEADBEEF, 0x12345678, 0xA5A5A5A5, 0x5A5A5A5A,
        0xC0FFEE00, 0xBADC0DE1, 0xFEEDFACE, 0xDEADC0DE,
        0x11111111, 0x22222222, 0x33333333, 0x44444444,
    ]

    def __init__(self, name: str = "PloverFirmwareConcurrentVSeq") -> None:
        super().__init__(name)

    async def body(self) -> None:
        await super().body()
        from firmware_bridge import run_hello_world

        master = _master_from_env(self)

        # Build the AXIS items and start their sub-sequence in the
        # background so it runs concurrently with the C firmware call.
        axis_seqr = self.p_sequencer.sub_seqrs["axis"]
        axis_items = [AxiStreamItem(data=w) for w in self.PATTERN]
        axis_seq = PloverAxisItemSeq(axis_items)
        axis_task = cocotb.start_soon(axis_seq.start(axis_seqr))

        include_dirs = _include_dirs_from_env()
        rc = await run_hello_world(
            master,
            shell_base=SHELL_BASE,
            syscon_base=SYSCON_BASE,
            expected_syscon_version=EXPECTED_SYSCON_VERSION,
            include_dirs=include_dirs,
        )
        assert rc == 0, f"plover_hello_world returned {rc} (non-zero = check failed)"

        # Concurrency probe: at least one AXIS beat should have landed
        # during the firmware execution.
        dut = cocotb.top
        mid_run_beats = int(dut.sink_beat_count.value)
        assert mid_run_beats > 0, (
            f"expected AXIS to have pushed beats during the C firmware "
            f"execution, but sink_beat_count = {mid_run_beats}")

        await axis_task


# ---- Helpers --------------------------------------------------------

def _master_from_env(vseq) -> "AxiLiteMaster":  # type: ignore[name-defined]
    """Reach the AxiLiteMaster the env's agent driver built.

    The agent's driver is lazy — it constructs the master on first
    drive_item. For firmware tests that bypass the sequencer, we call
    ``ensure_master()`` here so the BFM exists before the C wrapper
    starts invoking callbacks.
    """
    # The env is the p_sequencer's parent (.parent in pyuvm). We dug it
    # out via .get_parent() to avoid touching pyuvm internals more than
    # necessary.
    env = vseq.p_sequencer.get_parent()
    return env.axil_agent.driver.ensure_master()


def _include_dirs_from_env() -> list[Path]:
    raw = os.environ.get("PLOVER_RDL_INCLUDE_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


# ---- Base test -------------------------------------------------------

class PloverBaseTest(DVBaseTest):
    cfg_type = PloverEnvCfg
    env_type = PloverEnv

    # Subclasses set this when they expect a stream-side check at end-of-run.
    expect_axis_check: bool = False
    expected_beats: int = 0
    expected_xor: int = 0

    def __init__(self, name: str = "PloverBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverSmokeVSeq"
        self.settle_cycles = 3

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            await super().run_phase()
            # Give the AxiLite monitor a couple of cycles to drain any
            # in-flight B/R responses for transactions issued near the
            # end of the vseq body — otherwise the axil_count we log
            # could undercount by 1-2.
            if cocotb.is_simulation:
                await ClockCycles(cocotb.top.clk, 4)
            sb = self.env.scoreboard  # type: ignore[union-attr]
            self.logger.info(
                f"plover scoreboard: observed {sb.axil_count} AXI-Lite "
                f"transaction(s) on the bus")
            if self.expect_axis_check:
                await self._check_axis()
        finally:
            self.drop_objection()

    async def _check_axis(self) -> None:
        if not cocotb.is_simulation:
            return
        dut = cocotb.top
        await ClockCycles(dut.clk, self.settle_cycles)
        beat_count = int(dut.sink_beat_count.value)
        data_xor   = int(dut.sink_data_xor.value)
        sb = self.env.scoreboard  # type: ignore[union-attr]
        exp_beats = sb.exp_beats
        exp_xor   = sb.exp_xor
        self.logger.info(
            f"plover sink final state: beat_count={beat_count} "
            f"data_xor=0x{data_xor:08x} (expected beats={exp_beats}, "
            f"xor=0x{exp_xor:08x})")
        assert beat_count == exp_beats, (
            f"AXIS beat_count: got {beat_count}, expected {exp_beats}")
        assert data_xor == exp_xor, (
            f"AXIS data_xor: got 0x{data_xor:08x}, "
            f"expected 0x{exp_xor:08x}")


class PloverFirmwareSmokeTest(PloverBaseTest):
    def __init__(self, name: str = "PloverFirmwareSmokeTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverFirmwareSmokeVSeq"


class PloverFirmwareConcurrentTest(PloverBaseTest):
    expect_axis_check = True

    def __init__(self, name: str = "PloverFirmwareConcurrentTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverFirmwareConcurrentVSeq"
