"""
FuseSoC + pytest harness for the plover project-top testbench.

The ``plover`` core depends on ``axil_shell`` and ``counter``, so FuseSoC's
EDAM resolves the full source list (top integration + both sub-units) and
the cocotb runner builds the whole hierarchy.

Same shape as the unit harnesses under units/<unit>/dv/; the only knobs that
differ are the core name, the test module, and the source-back-mapping
(sources come from multiple unit dirs, not just one).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

CORE_NAME = "plover"
TEST_MODULE = "test_plover"
TESTCASES = ["smoke"]
RESOLVE_TARGET = "lint"

HERE = Path(__file__).resolve().parent          # plover/dv
PROJ_DIR = HERE.parent                          # plover/
ROOT = PROJ_DIR.parent                          # repo root

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
    """Map EDAM staged paths back to the live RTL.

    With a multi-core design, EDAM file names look like:
        src/plover_0.1.0/rtl/plover.sv
        src/axil_shell_0.1.0/rtl/axil_shell.sv
        src/counter_0.1.0/rtl/counter.sv

    We use the staged ``<core>_<ver>`` segment to choose the right live dir:
    ``plover_*`` -> ``plover/``, otherwise ``units/<core>/``.
    """
    hdl_types = {"verilogSource", "systemVerilogSource"}
    sources: list[Path] = []
    for f in edam.get("files", []):
        if f.get("file_type") not in hdl_types:
            continue
        parts = Path(f["name"]).parts
        # Find the staged core dir (the segment before "rtl/").
        if "rtl" not in parts:
            continue
        rtl_idx = parts.index("rtl")
        staged_core = parts[rtl_idx - 1]  # "<corename>_<ver>"
        rel = Path(*parts[rtl_idx:])      # "rtl/<file>"
        core_name = staged_core.rsplit("_", 1)[0]
        live_dir = PROJ_DIR if core_name == CORE_NAME else ROOT / "units" / core_name
        sources.append(live_dir / rel)
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
        test_dir=str(HERE),
        testcase=cocotb_testcase,
        waves=waves,
        extra_env={"PYTHONPATH": f"{HERE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_plover(design, cocotb_testcase):
    _run(design, cocotb_testcase)
