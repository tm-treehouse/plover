"""pytest configuration for the AXI-Lite shell testbench.

Puts ``dv/`` on ``sys.path`` so the ``axil_shell`` package imports during
collection, and exposes ``--sim``/``--waves`` options mirroring the ``SIM``
and ``WAVES`` env vars.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DV = Path(__file__).resolve().parent
if str(DV) not in sys.path:
    sys.path.insert(0, str(DV))


def pytest_addoption(parser):
    parser.addoption(
        "--sim",
        action="store",
        default=None,
        help="Simulator to use (verilator|icarus). Overrides $SIM.",
    )
    parser.addoption(
        "--waves",
        action="store_true",
        default=False,
        help="Dump waveforms during the run (also enabled by WAVES=1).",
    )


def pytest_configure(config):
    sim = config.getoption("--sim")
    if sim:
        os.environ["SIM"] = sim
    if config.getoption("--waves"):
        os.environ["WAVES"] = "1"
