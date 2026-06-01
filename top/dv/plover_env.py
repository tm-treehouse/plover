"""Plover top env: three agents + DSP-aware scoreboard.

After the chain integration, the top is:
* one AXI4-Lite host port (xbar splits to three slaves: axil_shell,
  syscon, fir_filter)
* one AXIS sample input (drives the CIC-FIR chain at the chain input
  rate)
* one AXIS sample output (filtered chain output, at input_rate / R)
* a counter debug output

The env composes the work of three agents:

* :class:`AxiLiteAgent` on ``s_axil_*`` — active. Drives register
  reads/writes. Its monitor is the system-wide observer of every
  AXI-Lite transaction on the bus (sequencer-driven AND C-driven via
  the firmware bridge).

* :class:`AxiStreamAgent` on ``s_axis_*`` — active, role="source".
  Drives sample beats into the chain at the input rate. Reuses the
  agent's source BFM; the monitor passively samples the bus.

* :class:`AxiStreamAgent` on ``m_axis_*`` — active, role="sink". Acts as
  the chain's output consumer (drives tready high); its monitor
  publishes every observed output sample to the scoreboard.

The scoreboard is genuinely DSP-aware. It maintains a CicFirChain
Python reference model and:

* Observes AXI-Lite writes to the FIR page; updates the model's
  coefficient bank in lock-step. This works for sequencer-driven AND
  C-firmware-driven coefficient writes — the passive bus monitor sees
  both equally, which is the OpenTitan-style invariant the project's
  agent design buys.
* Observes AXIS-in beats; calls ``model.step(sample)`` per beat,
  accumulating predicted outputs.
* Observes AXIS-out beats; compares each one against the next predicted
  output. Bit-exact match required (the chain's primitives are bit-exact
  individually, so the chain is bit-exact).

A test that programs different coefficient sequences and drives
different sample patterns gets a per-sample correctness check for free
— no need to do its own prediction.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import cocotb
from pyuvm import ConfigDB, uvm_tlm_analysis_fifo

from dv_lib import (
    DVBaseEnv, DVBaseEnvCfg, DVBaseScoreboard, DVBaseVirtualSequencer,
    UVM_ACTIVE,
)

from dv import (
    AxiLiteAgent, AxiLiteAgentCfg, AxiLiteItem, AxiLiteOp,
    AxiStreamAgent, AxiStreamAgentCfg, AxiStreamItem,
)

# Bring the DSP reference models into the env. ROOT is two levels up from
# top/dv/.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dv.dsp_models import CicFirChain  # noqa: E402


_log = logging.getLogger("dv_lib.plover")


# Address map constants (must match plover.sv's xbar SLAVE_BASE entries).
SHELL_BASE  = 0x0000_0000
SYSCON_BASE = 0x0000_1000
FIR_BASE    = 0x0000_2000
FIR_PAGE_MASK = 0xFFFF_F000


# DSP chain parameters used by the scoreboard's reference model. These
# must match the plover.sv parameter values (CIC_STAGES, CIC_DECIM, ...)
# at build time. The test wiring sets these via the env cfg in
# build_phase before the scoreboard runs.


class PloverScoreboard(DVBaseScoreboard):
    """DSP-aware scoreboard.

    Maintains a CicFirChain reference model that's kept in lock-step with
    the RTL via three streams of observed transactions:

    1. AxiLite writes to the FIR page  -> model.set_coef
    2. AxiStream-in beats              -> predicted = model.step
    3. AxiStream-out beats             -> compare to predicted

    Mismatches are logged with index + expected vs. got, counted into
    self.mismatches, and the base test asserts the count is zero at
    end-of-run.
    """

    def build_phase(self) -> None:
        super().build_phase()
        self.axil_fifo    = uvm_tlm_analysis_fifo("axil_fifo", self)
        self.axis_in_fifo = uvm_tlm_analysis_fifo("axis_in_fifo", self)
        self.axis_out_fifo = uvm_tlm_analysis_fifo("axis_out_fifo", self)
        # Reference model — actual parameters threaded in via env cfg
        # before run_phase starts. Defaults match the plover.sv defaults.
        self.cic_stages = 3
        self.cic_decim  = 4
        self.cic_delay  = 1
        self.sample_w   = 16
        self.fir_n_taps    = 8
        self.fir_coef_w    = 16
        self.fir_out_shift = 15
        self.model: Optional[CicFirChain] = None

        # Predicted-output queue. Items pushed in by _consume_axis_in and
        # drained by _consume_axis_out. Items are also passed through to
        # caller-visible attributes for end-of-test diagnostics.
        self._pred_out: list[int] = []
        self.axil_count    = 0
        self.fir_writes    = 0
        self.axis_in_count = 0
        self.axis_out_count = 0
        self.mismatches: list[tuple[int, int, int]] = []  # (idx, expected, got)
        # Per-test observation traces, kept for end-of-test plotting.
        # The scoreboard fills these as samples flow through; the base
        # test reads them after run_phase completes and hands them to
        # the dsp_plot helper to produce an HDL-vs-model PNG. Three
        # lists, all in the scoreboard's signed integer convention:
        #   * observed_inputs   — every AXIS-in beat (signed sample_w)
        #   * predicted_outputs — every prediction the model emitted
        #                          (one per CIC_DECIM inputs)
        #   * observed_outputs  — every AXIS-out beat
        # Always recorded (cheap), used by the base test only when
        # ``enable_chain_check`` and matplotlib is available.
        self.observed_inputs:   list[int] = []
        self.predicted_outputs: list[int] = []
        self.observed_outputs:  list[int] = []
        # Set by the test before run_phase to gate the AXIS-out comparison.
        # When False, samples are still observed and counted but not
        # compared (useful for sub-tests that want to drive samples
        # without programming coefficients first).
        self.compare_axis_out = True

    def _init_model(self) -> None:
        self.model = CicFirChain(
            cic_stages=self.cic_stages,
            cic_decim=self.cic_decim,
            cic_delay=self.cic_delay,
            cic_in_w=self.sample_w,
            cic_out_w=self.sample_w,
            fir_n_taps=self.fir_n_taps,
            fir_in_w=self.sample_w,
            fir_coef_w=self.fir_coef_w,
            fir_out_w=self.sample_w,
            fir_out_shift=self.fir_out_shift,
        )

    async def run_phase(self) -> None:
        if self.model is None:
            self._init_model()
        cocotb.start_soon(self._consume_axil())
        cocotb.start_soon(self._consume_axis_in())
        cocotb.start_soon(self._consume_axis_out())

    @staticmethod
    def _signed(value: int, width: int) -> int:
        v = value & ((1 << width) - 1)
        if v >> (width - 1):
            v -= (1 << width)
        return v

    async def _consume_axil(self) -> None:
        """Watch the AXI-Lite bus for FIR coefficient writes; update the
        model's coefficient bank in lock-step.

        Other AXI-Lite traffic (shell ID reads, syscon writes, DECERR
        probes) is counted for diagnostics but doesn't affect the
        chain model."""
        while True:
            item: AxiLiteItem = await self.axil_fifo.get()
            self.axil_count += 1
            if item.op != AxiLiteOp.WRITE:
                continue
            if (item.addr & FIR_PAGE_MASK) != FIR_BASE:
                continue
            # Coefficient bank: tap index = (addr - FIR_BASE) / 4
            tap_idx = (item.addr - FIR_BASE) >> 2
            if tap_idx >= self.fir_n_taps:
                # Out-of-range write — the RTL returns DECERR and doesn't
                # commit, so the model shouldn't either. Ignore.
                continue
            value = self._signed(item.data, self.fir_coef_w)
            if self.model is not None:
                self.model.set_coef(tap_idx, value)
            self.fir_writes += 1

    async def _consume_axis_in(self) -> None:
        """Observe input beats. Each beat is fed into the model;
        whenever the model produces an output (every R inputs), that
        prediction is queued for comparison against the matching
        observed output beat."""
        while True:
            item: AxiStreamItem = await self.axis_in_fifo.get()
            self.axis_in_count += 1
            sample = self._signed(item.data, self.sample_w)
            self.observed_inputs.append(sample)
            if self.model is None:
                continue
            predicted = self.model.step(sample)
            if predicted is not None:
                self._pred_out.append(predicted)
                self.predicted_outputs.append(predicted)

    async def _consume_axis_out(self) -> None:
        """Observe output beats. Compare each to the matching prediction.

        If the predicted queue is empty (output beat with no input
        prediction yet), record an over-production event — that's a
        chain bug. If the prediction queue grows unboundedly that's
        an under-production event; not reported per-beat but the
        end-of-run diagnostics surface it via remaining-prediction count.
        """
        while True:
            item: AxiStreamItem = await self.axis_out_fifo.get()
            got = self._signed(item.data, self.sample_w)
            idx = self.axis_out_count
            self.axis_out_count += 1
            self.observed_outputs.append(got)
            if not self.compare_axis_out:
                continue
            if not self._pred_out:
                self.mismatches.append((idx, 0, got))
                _log.error(
                    f"scoreboard: output beat {idx} = {got} but no "
                    "prediction in queue (RTL over-produced)")
                continue
            expected = self._pred_out.pop(0)
            if got != expected:
                self.mismatches.append((idx, expected, got))
                _log.error(
                    f"scoreboard: output beat {idx}: "
                    f"expected {expected}, got {got}")


# ---- Env cfg + env --------------------------------------------------

class PloverEnvCfg(DVBaseEnvCfg):
    """vif is the cocotb DUT handle; reused by all three agents."""

    def __init__(self, name: str = "plover_env_cfg") -> None:
        super().__init__(name)
        self.vif = None
        self.axil_agent_cfg: Optional[AxiLiteAgentCfg] = None
        self.axis_in_agent_cfg: Optional[AxiStreamAgentCfg] = None
        self.axis_out_agent_cfg: Optional[AxiStreamAgentCfg] = None
        # Chain parameters used to construct the scoreboard's model.
        self.cic_stages = 3
        self.cic_decim  = 4
        self.cic_delay  = 1
        self.sample_w   = 16
        self.fir_n_taps    = 8
        self.fir_coef_w    = 16
        self.fir_out_shift = 15

    def initialize(self, csr_base_addr: int = 0) -> None:
        super().initialize(csr_base_addr)
        self.axil_agent_cfg = AxiLiteAgentCfg("axil_agent_cfg")
        self.axil_agent_cfg.is_active = UVM_ACTIVE
        self.add_agent_cfg("axil", self.axil_agent_cfg)

        self.axis_in_agent_cfg = AxiStreamAgentCfg("axis_in_agent_cfg")
        self.axis_in_agent_cfg.is_active = UVM_ACTIVE
        self.axis_in_agent_cfg.role = "source"
        self.add_agent_cfg("axis_in", self.axis_in_agent_cfg)

        self.axis_out_agent_cfg = AxiStreamAgentCfg("axis_out_agent_cfg")
        self.axis_out_agent_cfg.is_active = UVM_ACTIVE
        self.axis_out_agent_cfg.role = "sink"
        self.add_agent_cfg("axis_out", self.axis_out_agent_cfg)


class PloverVirtualSequencer(DVBaseVirtualSequencer):
    pass


class PloverEnv(DVBaseEnv):
    cfg_type = PloverEnvCfg
    scoreboard_type = PloverScoreboard
    virtual_sequencer_type = PloverVirtualSequencer

    def __init__(self, name: str = "plover_env", parent=None) -> None:
        super().__init__(name, parent)
        self.axil_agent: Optional[AxiLiteAgent] = None
        self.axis_in_agent: Optional[AxiStreamAgent] = None
        self.axis_out_agent: Optional[AxiStreamAgent] = None

    def build_phase(self) -> None:
        super().build_phase()
        cfg: PloverEnvCfg = self.cfg  # type: ignore[assignment]
        assert cfg.axil_agent_cfg is not None
        assert cfg.axis_in_agent_cfg is not None
        assert cfg.axis_out_agent_cfg is not None
        # Propagate vif to every agent cfg before instantiation.
        cfg.axil_agent_cfg.vif = cfg.vif
        cfg.axis_in_agent_cfg.vif = cfg.vif
        cfg.axis_in_agent_cfg.prefix = "s_axis"
        cfg.axis_out_agent_cfg.vif = cfg.vif
        cfg.axis_out_agent_cfg.prefix = "m_axis"

        ConfigDB().set(self, "axil_agent",     "cfg", cfg.axil_agent_cfg)
        ConfigDB().set(self, "axis_in_agent",  "cfg", cfg.axis_in_agent_cfg)
        ConfigDB().set(self, "axis_out_agent", "cfg", cfg.axis_out_agent_cfg)
        self.axil_agent     = AxiLiteAgent.create("axil_agent", self)
        self.axis_in_agent  = AxiStreamAgent.create("axis_in_agent", self)
        self.axis_out_agent = AxiStreamAgent.create("axis_out_agent", self)

        # Thread chain parameters through to the scoreboard so its model
        # is parameterised identically to the RTL.
        sb: PloverScoreboard = self.scoreboard  # type: ignore[assignment]
        sb.cic_stages = cfg.cic_stages
        sb.cic_decim  = cfg.cic_decim
        sb.cic_delay  = cfg.cic_delay
        sb.sample_w   = cfg.sample_w
        sb.fir_n_taps    = cfg.fir_n_taps
        sb.fir_coef_w    = cfg.fir_coef_w
        sb.fir_out_shift = cfg.fir_out_shift

    def connect_phase(self) -> None:
        super().connect_phase()
        assert self.axil_agent is not None
        assert self.axis_in_agent is not None
        assert self.axis_out_agent is not None
        sb: PloverScoreboard = self.scoreboard  # type: ignore[assignment]
        self.axil_agent.monitor.analysis_port.connect(sb.axil_fifo.analysis_export)
        self.axis_in_agent.monitor.analysis_port.connect(sb.axis_in_fifo.analysis_export)
        self.axis_out_agent.monitor.analysis_port.connect(sb.axis_out_fifo.analysis_export)
        if self.axil_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axil", self.axil_agent.sequencer)
        if self.axis_in_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axis_in", self.axis_in_agent.sequencer)
        # axis_out_agent has no useful sequencer (sink role), but register
        # if dv_lib instantiated one — harmless.
        if self.axis_out_agent.sequencer is not None:
            self.virtual_sequencer.register_seqr("axis_out", self.axis_out_agent.sequencer)
