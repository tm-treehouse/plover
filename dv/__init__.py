"""Project-local DV components for plover testbenches.

This directory holds protocol-specific agents (AXI-Lite, AXI-Stream) and any
other shared DV machinery that doesn't belong in the upstream pyuvm-dv-lib.

Why this is separate from pyuvm-dv-lib:
    pyuvm-dv-lib is a port of OpenTitan's *dv_lib* — just the base-class
    skeleton. Protocol agents in OpenTitan live in *cip_lib*, which is not
    part of pyuvm-dv-lib. The equivalent for this project is here.

Why this is separate from tools/:
    tools/ holds build-time scripts (RDL generators, version-header
    generator) invoked at FuseSoC time. The DV agents are runtime
    components consumed by the testbenches. Keeping them apart keeps each
    directory's role clear.

Active vs passive mode:
    Both agents work in either mode via the base ``DVBaseAgent`` logic
    keyed on ``cfg.is_active``:

    * ``UVM_ACTIVE``   — creates monitor + sequencer + driver. The
                          default; use when this agent drives stimulus.
    * ``UVM_PASSIVE``  — creates only the monitor. Use when you want to
                          observe an external master (e.g. monitoring
                          the downstream side of the xbar from a
                          top-level test) without driving stimulus.

    Set the mode on the agent cfg before adding it to the env::

        cfg.axil_agent_cfg = AxiLiteAgentCfg(\"axil_obs_cfg\")
        cfg.axil_agent_cfg.is_active = UVM_PASSIVE
        cfg.axil_agent_cfg.prefix = \"m_axil_0\"  # downstream port to observe
"""
from dv_lib import UVM_ACTIVE, UVM_PASSIVE

from dv.axi_lite_agent import (
    AxiLiteOp, AxiLiteItem, AxiLiteAgentCfg, AxiLiteAgent,
)
from dv.axi_stream_agent import (
    AxiStreamItem, AxiStreamAgentCfg, AxiStreamAgent,
)

# Driver and Monitor classes (AxiLiteDriver, AxiLiteMonitor,
# AxiStreamDriver, AxiStreamMonitor) are intentionally NOT re-exported
# here: nothing in the project imports them directly. The Agent class
# instantiates them by name lookup. Anyone needing them for a factory
# override can still import from dv.axi_lite_agent / dv.axi_stream_agent.

__all__ = [
    "UVM_ACTIVE", "UVM_PASSIVE",
    "AxiLiteOp", "AxiLiteItem", "AxiLiteAgentCfg", "AxiLiteAgent",
    "AxiStreamItem", "AxiStreamAgentCfg", "AxiStreamAgent",
]
