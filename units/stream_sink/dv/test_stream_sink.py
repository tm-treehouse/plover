"""cocotb entry point for the stream_sink unit testbench.

Same shape as test_counter.py and test_axil_shell.py: a small mixin wires
the live DUT handle + ClkRstIf into the env cfg in ``build_phase``, then
defers to the dv_lib base. The run-phase objection AND the end-of-run
beat_count / data_xor comparison live in :class:`StreamSinkBaseTest`.

Tests exposed to cocotb (selectable with pytest's ``-k``):
* ``smoke`` -> StreamSinkSmokeVSeq
"""
from __future__ import annotations

import cocotb
import pyuvm
from pyuvm import ConfigDB

from dv_lib import ClkRstIf

from stream_sink_env import StreamSinkEnvCfg
from stream_sink_test import StreamSinkBaseTest
import stream_sink_test  # noqa: F401  -- registers vseq by name

CLK_PERIOD_NS = 10
AXIS_PREFIX = "s_axis"


class _CfgWiringMixin:
    def build_phase(self) -> None:
        dut = cocotb.top
        cfg = StreamSinkEnvCfg("stream_sink_env_cfg")
        cfg.initialize()
        cfg.vif = dut
        assert cfg.axis_agent_cfg is not None
        cfg.axis_agent_cfg.vif = dut
        cfg.axis_agent_cfg.prefix = AXIS_PREFIX

        cfg.clk_rst_vif = ClkRstIf(dut.clk, dut.rst_n,
                                   period_ns=CLK_PERIOD_NS,
                                   reset_active_low=True)
        cfg.clk_rst_vif.start_clk()

        ConfigDB().set(self, "env", "cfg", cfg)
        super().build_phase()


@pyuvm.test()
class smoke(_CfgWiringMixin, StreamSinkBaseTest):
    pass
