"""Plover top test classes + virtual sequences.

After the DSP chain integration, the top exposes:
  * an AXI4-Lite port (xbar to axil_shell / syscon / fir_filter)
  * an AXIS-in port carrying samples into the CIC-FIR chain
  * an AXIS-out port carrying the filtered chain output
  * a counter debug output

Tests come in two flavours:

* Control-plane tests (smoke, firmware_smoke, firmware_program_fir)
  exercise the register interfaces — known-value reads, DECERR probes,
  counter enable/freeze, soft-reset gating, plus the C firmware path.
  These are the "integration is wired" checks.

* DSP-aware tests (chain_impulse, chain_tone) feed signals into the
  chain and rely on the DSP-aware scoreboard to verify the chain
  output sample-by-sample against a bit-exact reference model. The
  scoreboard tracks coefficient writes off the AXI-Lite bus monitor,
  so the test doesn't need to manually keep model and DUT in sync.

Sequences carry signal info: ``ChainImpulseVSeq`` programs the FIR
with a delta filter and drives an impulse stream; ``ChainToneVSeq``
programs a configurable averager and drives a sinusoidal stream.
Future signal types (chirps, noise, multi-tone) are small additions.

The base test asserts the scoreboard's mismatch count is zero at
end-of-run for the DSP-aware tests; for control-plane tests it logs
the AXI-Lite transaction count and leaves the scoreboard's chain
comparison off via cfg.compare_axis_out = False.
"""
from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path
from typing import Optional

import cocotb
from cocotb.triggers import ClockCycles, RisingEdge

from dv_lib import DVBaseTest, DVBaseVSeq, DVBaseSequence

from dv import AxiLiteItem, AxiLiteOp, AxiStreamItem

from plover_env import (
    PloverEnv, PloverEnvCfg,
    SHELL_BASE, SYSCON_BASE, FIR_BASE,
)

# Bring the dsp_plot helper into scope so DSP-aware tests can write a
# top-level HDL-vs-model comparison PNG at end of run, same shape as the
# per-unit plots under build/dsp_plots/.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from dv.dsp_plot import plot_test_result  # noqa: E402


# ---- Address-map constants -----------------------------------------

UNMAPPED = 0x0000_4000

SHELL_ID_OFFSET         = 0x0C
SHELL_ID_EXPECTED       = 0xC0C07B01
SHELL_CONTROL_OFFSET    = 0x04
SYSCON_VERSION_OFFSET   = 0x00
SYSCON_SOFT_RST_OFFSET  = 0x08
EXPECTED_SYSCON_VERSION = 0xCAFE_F00D
SYSCON_SOFT_RST_CYCLES  = 8

RESP_OKAY   = 0
RESP_DECERR = 3


# ---- Item sub-sequences --------------------------------------------

class PloverAxilItemSeq(DVBaseSequence):
    def __init__(self, items, name: str = "plover_axil_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


class PloverAxisItemSeq(DVBaseSequence):
    def __init__(self, items, name: str = "plover_axis_item_seq") -> None:
        super().__init__(name)
        self._items = items

    async def body(self) -> None:
        for item in self._items:
            await self.start_item(item)
            await self.finish_item(item)


# ---- Control-plane vseqs (smoke etc.) ------------------------------

class PloverSmokeVSeq(DVBaseVSeq):
    """Register-level integration smoke: known-value reads, DECERR,
    CONTROL.ENABLE gating of the counter, soft-reset window.

    Does NOT stimulate the DSP chain. The base test should leave the
    scoreboard's chain comparison off for this test.
    """

    async def body(self) -> None:
        await super().body()
        seqr = self.p_sequencer.sub_seqrs["axil"]
        dut = cocotb.top

        # 1) Read shell.ID and syscon.VERSION.
        items = [AxiLiteItem(op=AxiLiteOp.READ, addr=SHELL_BASE + SHELL_ID_OFFSET)]
        await PloverAxilItemSeq(items).start(seqr)
        assert items[0].resp == RESP_OKAY, f"shell read resp {items[0].resp}"
        assert items[0].data == SHELL_ID_EXPECTED, (
            f"axil_shell.ID via xbar: got 0x{items[0].data:08x}, "
            f"expected 0x{SHELL_ID_EXPECTED:08x}")

        items = [AxiLiteItem(op=AxiLiteOp.READ, addr=SYSCON_BASE + SYSCON_VERSION_OFFSET)]
        await PloverAxilItemSeq(items).start(seqr)
        assert items[0].resp == RESP_OKAY, f"syscon read resp {items[0].resp}"
        assert items[0].data == EXPECTED_SYSCON_VERSION, (
            f"syscon.VERSION via xbar: got 0x{items[0].data:08x}, "
            f"expected 0x{EXPECTED_SYSCON_VERSION:08x}")

        # 2) Unmapped address returns DECERR for write and read.
        items = [
            AxiLiteItem(op=AxiLiteOp.WRITE, addr=UNMAPPED, data=0xDEADBEEF),
            AxiLiteItem(op=AxiLiteOp.READ,  addr=UNMAPPED),
        ]
        await PloverAxilItemSeq(items).start(seqr)
        for i, exp_op in enumerate(("write", "read")):
            assert items[i].resp == RESP_DECERR, (
                f"{exp_op} to unmapped 0x{UNMAPPED:08x}: "
                f"got resp {items[i].resp}, expected DECERR (3)")

        # 3) CONTROL.ENABLE gates the counter. After reset ENABLE=0.
        await ClockCycles(dut.clk, 5)
        held = int(dut.count.value)
        assert held == 0, f"counter pre-enable: got 0x{held:x}, expected 0"

        await PloverAxilItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE,
                        addr=SHELL_BASE + SHELL_CONTROL_OFFSET, data=1),
        ]).start(seqr)
        await RisingEdge(dut.clk)
        start = int(dut.count.value)
        await ClockCycles(dut.clk, 10)
        end = int(dut.count.value)
        mask = (1 << len(dut.count)) - 1
        assert ((end - start) & mask) == 10, (
            f"counter advance: got {((end - start) & mask)}, expected 10")

        await PloverAxilItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE,
                        addr=SHELL_BASE + SHELL_CONTROL_OFFSET, data=0),
        ]).start(seqr)
        await ClockCycles(dut.clk, 2)
        a = int(dut.count.value)
        await ClockCycles(dut.clk, 10)
        b = int(dut.count.value)
        assert a == b, f"counter freeze: 0x{a:x} -> 0x{b:x} (expected unchanged)"

        # Re-enable so the soft-reset check below sees movement.
        await PloverAxilItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE,
                        addr=SHELL_BASE + SHELL_CONTROL_OFFSET, data=1),
        ]).start(seqr)

        # 4) Soft-reset window.
        await PloverAxilItemSeq([
            AxiLiteItem(op=AxiLiteOp.WRITE,
                        addr=SYSCON_BASE + SYSCON_SOFT_RST_OFFSET, data=1),
        ]).start(seqr)
        await ClockCycles(dut.clk, 3)
        mid = int(dut.count.value)
        assert mid == 0, f"soft-reset gating: counter not held (0x{mid:x})"
        await ClockCycles(dut.clk, SYSCON_SOFT_RST_CYCLES + 2)
        after = int(dut.count.value)
        assert 1 <= after <= SYSCON_SOFT_RST_CYCLES + 4, (
            f"soft-reset release: small count expected, got 0x{after:x}")


# ---- Firmware-driven vseqs -----------------------------------------

class PloverFirmwareSmokeVSeq(DVBaseVSeq):
    """Call the C ``plover_hello_world`` via the firmware bridge."""

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
        assert rc == 0, f"plover_hello_world returned {rc}"


class PloverFirmwareProgramFirVSeq(DVBaseVSeq):
    """C programs the FIR coefficient bank via the bridge, then the
    sequence pushes samples through the chain. The scoreboard tracks
    the C-driven coefficient writes via its passive AXI-Lite monitor —
    no explicit sync between test and scoreboard required."""

    # Coefficient set: a unity-gain averager (each tap = max/N), so the
    # filtered output is a sliding average of the CIC-decimated stream.
    # Chosen because it's nontrivial (all taps used) but easy to eyeball.
    @staticmethod
    def averaging_coefs(n_taps: int, coef_w: int) -> list[int]:
        unit = ((1 << (coef_w - 1)) - 1) // n_taps
        return [unit] * n_taps

    async def body(self) -> None:
        await super().body()
        from firmware_bridge import run_program_fir
        master = _master_from_env(self)
        include_dirs = _include_dirs_from_env()

        env_cfg: PloverEnvCfg = self.p_sequencer.get_parent().cfg  # type: ignore[attr-defined]
        coefs = self.averaging_coefs(env_cfg.fir_n_taps, env_cfg.fir_coef_w)

        rc = await run_program_fir(
            master, fir_base=FIR_BASE, coefs=coefs,
            verify_readback=True, include_dirs=include_dirs)
        assert rc == 0, f"plover_program_fir returned {rc}"

        # Push samples through the chain. The scoreboard verifies each
        # output beat against the CicFirChain model; the model picked up
        # the coefficient programming via the bus monitor.
        seqr_in = self.p_sequencer.sub_seqrs["axis_in"]
        rng = random.Random(0xC0FFEE)
        sample_w = env_cfg.sample_w
        decim   = env_cfg.cic_decim
        # Enough samples to produce ~12 output beats.
        n_inputs = decim * 12
        lo = -(1 << (sample_w - 1))
        hi =  (1 << (sample_w - 1)) - 1
        # Use the sample_w bit mask so the AXIS BFM picks up the right
        # signed integer payload.
        mask = (1 << sample_w) - 1
        items = [AxiStreamItem(data=rng.randint(lo, hi) & mask)
                 for _ in range(n_inputs)]
        await PloverAxisItemSeq(items).start(seqr_in)
        # Let the chain drain.
        await ClockCycles(cocotb.top.clk, 200)


# ---- DSP-aware vseqs (signal-carrying) -----------------------------

class ChainImpulseVSeq(DVBaseVSeq):
    """Program a delta filter (coef[0]=max, rest=0) so the FIR is a
    one-cycle pass-through, then drive an impulse stream. The CIC
    decimator's impulse response gets passed unmodified through the
    FIR, so the chain output is identical to a standalone CIC's impulse
    response.
    """

    async def body(self) -> None:
        await super().body()
        seqr_axil = self.p_sequencer.sub_seqrs["axil"]
        seqr_axis = self.p_sequencer.sub_seqrs["axis_in"]
        env_cfg: PloverEnvCfg = self.p_sequencer.get_parent().cfg  # type: ignore[attr-defined]
        n_taps = env_cfg.fir_n_taps
        coef_w = env_cfg.fir_coef_w
        sample_w = env_cfg.sample_w
        decim   = env_cfg.cic_decim

        # Delta filter: coef[0] = max-positive, rest = 0.
        c0 = (1 << (coef_w - 1)) - 1
        writes = [AxiLiteItem(op=AxiLiteOp.WRITE,
                              addr=FIR_BASE + 4 * i,
                              data=(c0 if i == 0 else 0))
                  for i in range(n_taps)]
        await PloverAxilItemSeq(writes).start(seqr_axil)

        # Impulse input: one nonzero sample, then zeros.
        amplitude = (1 << (sample_w - 2))  # 0.5 in Q1.(sample_w-1)
        mask = (1 << sample_w) - 1
        n_inputs = decim * 16
        inputs = [0] * n_inputs
        inputs[0] = amplitude
        items = [AxiStreamItem(data=s & mask) for s in inputs]
        await PloverAxisItemSeq(items).start(seqr_axis)
        # Let the chain drain.
        await ClockCycles(cocotb.top.clk, 200)


class ChainToneVSeq(DVBaseVSeq):
    """Drive a sinusoidal tone through the chain. Programs an averager
    so the FIR does real lowpass filtering on the decimated tone.

    Sequence parameters carry signal info:
      * frequency (cycles per input sample, normalised: 0..0.5)
      * amplitude (signed integer in the sample_w range)
      * num_inputs (length of the stimulus stream)
    """

    def __init__(self, name: str = "ChainToneVSeq") -> None:
        super().__init__(name)
        # Defaults — testbenches can override before start().
        self.freq_norm = 0.05       # cycles per input sample
        self.amplitude_frac = 0.5   # fraction of full-scale
        self.num_inputs = 256       # samples to stream

    async def body(self) -> None:
        await super().body()
        seqr_axil = self.p_sequencer.sub_seqrs["axil"]
        seqr_axis = self.p_sequencer.sub_seqrs["axis_in"]
        env_cfg: PloverEnvCfg = self.p_sequencer.get_parent().cfg  # type: ignore[attr-defined]
        n_taps = env_cfg.fir_n_taps
        coef_w = env_cfg.fir_coef_w
        sample_w = env_cfg.sample_w

        # Unity-gain averager: each tap = max/N (truncated).
        unit = ((1 << (coef_w - 1)) - 1) // n_taps
        writes = [AxiLiteItem(op=AxiLiteOp.WRITE,
                              addr=FIR_BASE + 4 * i, data=unit)
                  for i in range(n_taps)]
        await PloverAxilItemSeq(writes).start(seqr_axil)

        # Generate a sinusoidal input. amplitude is a fraction of
        # full-scale so it's headroom-safe even when amplified through
        # the CIC's gain.
        amp = int(self.amplitude_frac * ((1 << (sample_w - 1)) - 1))
        mask = (1 << sample_w) - 1
        samples = [int(amp * math.sin(2 * math.pi * self.freq_norm * n))
                   for n in range(self.num_inputs)]
        items = [AxiStreamItem(data=s & mask) for s in samples]
        await PloverAxisItemSeq(items).start(seqr_axis)
        await ClockCycles(cocotb.top.clk, 200)


# ---- Helpers --------------------------------------------------------

def _master_from_env(vseq):
    """Reach the AxiLiteMaster the env's agent driver built."""
    env = vseq.p_sequencer.get_parent()
    return env.axil_agent.driver.ensure_master()


def _include_dirs_from_env() -> list[Path]:
    raw = os.environ.get("PLOVER_RDL_INCLUDE_DIRS", "")
    return [Path(p) for p in raw.split(os.pathsep) if p]


# ---- Base test -------------------------------------------------------

class PloverBaseTest(DVBaseTest):
    cfg_type = PloverEnvCfg
    env_type = PloverEnv

    # Subclasses can override; controls whether the scoreboard does the
    # per-beat chain comparison at end-of-run.
    enable_chain_check: bool = False
    # Drain cycles between end of vseq and final asserts.
    drain_cycles: int = 8
    # Plot filename slug used when enable_chain_check is True. Subclasses
    # override per-test so each plot lands at a distinct path under
    # build/dsp_plots/. Default falls back to the UVM component name so
    # an unconfigured subclass still produces *something* useful.
    plot_name: str = "plover_top"

    def __init__(self, name: str = "PloverBaseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverSmokeVSeq"

    def _plot_chain(self) -> None:
        """Write a three-panel HDL-vs-model comparison PNG. Same shape
        as the unit-level DSP plots, but composed from the scoreboard's
        recorded observation traces: inputs from AXIS-in, expected
        from the CicFirChain model, got from AXIS-out.

        Plot is best-effort — it never fails the test. matplotlib
        unavailable (or any plot helper exception) just skips the
        write."""
        sb = self.env.scoreboard  # type: ignore[union-attr]
        cfg: PloverEnvCfg = self.env.cfg  # type: ignore[union-attr]
        try:
            title = (
                f"plover top {self.plot_name}: "
                f"CIC N={cfg.cic_stages} R={cfg.cic_decim} M={cfg.cic_delay}, "
                f"FIR N_TAPS={cfg.fir_n_taps}, "
                f"COEF_W={cfg.fir_coef_w}, SAMPLE_W={cfg.sample_w}")
            path = plot_test_result(
                filename=f"plover_top__{self.plot_name}",
                title=title,
                inputs=sb.observed_inputs,
                expected=sb.predicted_outputs,
                got=sb.observed_outputs,
                # Input rate is CIC_DECIM times the output rate, so
                # scale the input x-axis by 1/R for visual alignment
                # with the output traces — same convention as the
                # unit cic_decimator plots.
                input_rate_ratio=1.0 / max(cfg.cic_decim, 1),
                output_label="chain output (CIC -> FIR)",
            )
            if path is not None:
                self.logger.info(f"chain plot written to {path}")
        except Exception as ex:  # noqa: BLE001 — never fail the test on plot
            self.logger.warning(f"chain plot skipped: {ex!r}")

    async def run_phase(self) -> None:
        self.raise_objection()
        try:
            # Configure scoreboard mode before its consumers start
            # comparing. PloverScoreboard.compare_axis_out defaults to
            # True; control-plane tests turn it off.
            sb = self.env.scoreboard  # type: ignore[union-attr]
            sb.compare_axis_out = bool(self.enable_chain_check)
            await super().run_phase()
            # Drain so any in-flight AXIS-out beats are observed.
            if cocotb.is_simulation:
                await ClockCycles(cocotb.top.clk, self.drain_cycles)
            self.logger.info(
                f"plover scoreboard: axil_count={sb.axil_count} "
                f"fir_writes={sb.fir_writes} "
                f"axis_in_count={sb.axis_in_count} "
                f"axis_out_count={sb.axis_out_count}")
            if self.enable_chain_check:
                # Write the plot BEFORE asserting, so a failed test
                # still produces a comparison PNG showing the divergence
                # — same pattern as the unit-level DSP tests.
                self._plot_chain()
                assert not sb.mismatches, (
                    f"chain scoreboard: {len(sb.mismatches)} mismatch(es); "
                    f"first: idx={sb.mismatches[0][0]} "
                    f"expected={sb.mismatches[0][1]} got={sb.mismatches[0][2]}")
                # Leftover predictions indicate the chain under-produced
                # (samples came in but no output was emitted).
                if sb._pred_out:
                    self.logger.warning(
                        f"chain scoreboard: {len(sb._pred_out)} predicted "
                        "output(s) had no matching DUT output (under-produced)")
        finally:
            self.drop_objection()


class PloverFirmwareSmokeTest(PloverBaseTest):
    def __init__(self, name: str = "PloverFirmwareSmokeTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverFirmwareSmokeVSeq"


class PloverFirmwareProgramFirTest(PloverBaseTest):
    enable_chain_check = True
    plot_name = "firmware_program_fir"

    def __init__(self, name: str = "PloverFirmwareProgramFirTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "PloverFirmwareProgramFirVSeq"


class PloverChainImpulseTest(PloverBaseTest):
    enable_chain_check = True
    plot_name = "chain_impulse"

    def __init__(self, name: str = "PloverChainImpulseTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "ChainImpulseVSeq"


class PloverChainToneTest(PloverBaseTest):
    enable_chain_check = True
    plot_name = "chain_tone"

    def __init__(self, name: str = "PloverChainToneTest", parent=None) -> None:
        super().__init__(name, parent)
        self.test_seq_s = "ChainToneVSeq"
