"""Repo-root pytest configuration.

Exposes ``--sim`` / ``--waves`` options mirroring the ``SIM`` / ``WAVES`` env
vars. Each unit's harness (under ``units/<unit>/dv/test_*_pytest.py``) sets
its own ``PYTHONPATH`` for its ``dv/`` package via cocotb's ``extra_env``.
"""
from __future__ import annotations

import os


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
