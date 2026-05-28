"""
FuseSoC + pytest harness for the counter sub-unit. **Template** — this file
intentionally mirrors ``dv/test_axil_shell_pytest.py``. To add another
unit, copy this file, change CORE_NAME / TEST_MODULE / TESTCASES, and adjust
BUILD_ARGS / parameters if needed.

Keeping each unit's harness self-contained (rather than factoring shared
helpers) means a new unit is one copyable file plus a .core and a dv/<unit>/
package — easier to read and adapt than a shared driver.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

# ---- Unit-specific knobs (the bits you change when adapting) --------
CORE_NAME = "counter"
TEST_MODULE = "counter.test_counter"
TESTCASES = ["smoke", "clear"]

# ---- Shared boilerplate (same shape as the shell harness) -----------
ROOT = Path(__file__).resolve().parents[1]
DV = ROOT / "dv"
# Source-resolution target. Must have a default_tool; "lint" is the natural
# choice since every unit's .core in this repo already has it.
RESOLVE_TARGET = "lint"

BUILD_ARGS = {
    "verilator": ["--trace", "--trace-structs",
                  "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
                  "-Wno-WIDTHEXPAND"],
    "icarus": ["-g2012"],
}


def _resolve_verilator_root() -> None:
    try:
        import verilator  # type: ignore
    except Exception:
        return
    pkg = Path(verilator.__file__).resolve().parent
    if (pkg / "bin" / "verilator").exists():
        os.environ["VERILATOR_ROOT"] = str(pkg)
        os.environ["PATH"] = f"{pkg / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"


def _fusesoc_edam() -> dict:
    subprocess.run(
        ["fusesoc", "run", "--target", RESOLVE_TARGET, "--setup", CORE_NAME],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    candidates = sorted(
        (ROOT / "build").rglob(f"{CORE_NAME}_*/*/*.eda.yml"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        pytest.skip("FuseSoC did not produce an EDAM file; is fusesoc installed?")
    return yaml.safe_load(candidates[-1].read_text())


def _sources_from_edam(edam: dict) -> tuple[list[Path], str]:
    hdl_types = {"verilogSource", "systemVerilogSource"}
    sources: list[Path] = []
    for f in edam.get("files", []):
        if f.get("file_type") in hdl_types:
            tail = Path(f["name"])
            parts = tail.parts
            if "rtl" in parts:
                rel = Path(*parts[parts.index("rtl"):])
            else:
                rel = Path(tail.name)
            sources.append(ROOT / rel)
    toplevel = edam.get("toplevel", CORE_NAME)
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return sources, toplevel


@pytest.fixture(scope="module")
def design():
    edam = _fusesoc_edam()
    sources, toplevel = _sources_from_edam(edam)
    assert sources, f"no HDL sources resolved from FuseSoC EDAM for {CORE_NAME}"
    return {"sources": sources, "toplevel": toplevel}


def _run(design, cocotb_testcase: str) -> None:
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        _resolve_verilator_root()
    waves = os.getenv("WAVES", "0") not in ("0", "", "false", "False")

    runner = get_runner(sim)
    runner.build(
        sources=design["sources"],
        hdl_toplevel=design["toplevel"],
        build_args=BUILD_ARGS.get(sim, []),
        waves=waves,
        always=True,
    )
    runner.test(
        hdl_toplevel=design["toplevel"],
        test_module=TEST_MODULE,
        test_dir=str(DV),
        testcase=cocotb_testcase,
        waves=waves,
        extra_env={"PYTHONPATH": f"{DV}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_counter(design, cocotb_testcase):
    _run(design, cocotb_testcase)
