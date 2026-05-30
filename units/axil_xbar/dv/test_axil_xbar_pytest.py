"""FuseSoC + pytest harness for axil_xbar.

Builds a DV-only toplevel (axil_xbar_dv_top) that wires the xbar to two
behavioural RAM stubs at 0x0000 and 0x1000. The xbar itself is what's
being verified; the RAM stubs are test infrastructure.

Three testcases × three (INPUT_REG_STAGES, OUTPUT_REG_STAGES)
configurations = 9 parametrized runs. The configurations cover:
  (0, 0) — fully combinational (default)
  (1, 0) — one input stage (timing closure on the host-side fan-in)
  (0, 1) — one output stage (timing closure on the slave-side fan-out)
We don't sweep deeper stages today: the skid_buffer chains them, so DEPTH=2
is just a longer pipeline of DEPTH=1 stages, with no new logic paths.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

CORE_NAME = "axil_xbar"
TEST_MODULE = "test_axil_xbar"
TESTCASES = ["smoke", "decerr", "concurrent"]
STAGE_CONFIGS = [(0, 0), (1, 0), (0, 1)]
RESOLVE_TARGET = "lint"
DV_TOP = "axil_xbar_dv_top"

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]

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


def _sources_from_edam(edam: dict) -> list[Path]:
    hdl_types = {"verilogSource", "systemVerilogSource"}
    sources: list[Path] = []
    for f in edam.get("files", []):
        if f.get("file_type") not in hdl_types:
            continue
        if f.get("is_include_file"):
            continue
        parts = Path(f["name"]).parts
        if parts and parts[0] == "src" and len(parts) >= 3:
            core_dir = parts[1].rsplit("_", 1)[0]
            rel = Path(*parts[2:])
            sources.append(ROOT / "units" / core_dir / rel)
    # Pull in DV-only sources (axil_ram_stub.sv, axil_xbar_dv_top.sv) that
    # aren't part of the synthesizable RTL fileset.
    sources.append(UNIT_DIR / "dv" / "axil_ram_stub.sv")
    sources.append(UNIT_DIR / "dv" / "axil_xbar_dv_top.sv")
    return sources


@pytest.fixture(scope="module")
def design():
    edam = _fusesoc_edam()
    sources = _sources_from_edam(edam)
    assert sources, f"no HDL sources resolved for {CORE_NAME}"
    return {"sources": sources}


def _run(design, cocotb_testcase: str, in_stages: int, out_stages: int) -> None:
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        _resolve_verilator_root()
    waves = os.getenv("WAVES", "0") not in ("0", "", "false", "False")

    runner = get_runner(sim)
    runner.build(
        sources=design["sources"],
        hdl_toplevel=DV_TOP,
        build_args=BUILD_ARGS.get(sim, []),
        parameters={
            "INPUT_REG_STAGES":  in_stages,
            "OUTPUT_REG_STAGES": out_stages,
        },
        waves=waves,
        always=True,
    )
    runner.test(
        hdl_toplevel=DV_TOP,
        test_module=TEST_MODULE,
        test_dir=str(HERE),
        testcase=cocotb_testcase,
        waves=waves,
        extra_env={"PYTHONPATH": f"{HERE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
@pytest.mark.parametrize("stages", STAGE_CONFIGS,
                          ids=lambda s: f"in{s[0]}_out{s[1]}")
def test_axil_xbar(design, cocotb_testcase, stages):
    _run(design, cocotb_testcase, stages[0], stages[1])
