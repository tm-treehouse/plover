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
"""
from dv.axi_lite_agent import (
    AxiLiteOp, AxiLiteItem, AxiLiteAgentCfg, AxiLiteDriver, AxiLiteMonitor,
    AxiLiteAgent,
)
from dv.axi_stream_agent import (
    AxiStreamItem, AxiStreamAgentCfg, AxiStreamDriver, AxiStreamMonitor,
    AxiStreamAgent,
)

__all__ = [
    "AxiLiteOp", "AxiLiteItem", "AxiLiteAgentCfg",
    "AxiLiteDriver", "AxiLiteMonitor", "AxiLiteAgent",
    "AxiStreamItem", "AxiStreamAgentCfg",
    "AxiStreamDriver", "AxiStreamMonitor", "AxiStreamAgent",
]
