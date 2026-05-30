"""FuseSoC + pytest harness for the stream_sink unit. Same shape as other
units; no RDL involved (this block has no register file)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

CORE_NAME = "stream_sink"
TEST_MODULE = "test_stream_sink"
TESTCASES = ["smoke"]
RESOLVE_TARGET = "lint"

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]

BUILD_ARGS = {
    "verilator": ["--trace", "--trace-structs",
                  "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL"],
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
        if f.get("file_type") not in hdl_types:
            continue
        if f.get("is_include_file"):
            continue
        parts = Path(f["name"]).parts
        if parts and parts[0] == "src" and len(parts) >= 3:
            core_dir = parts[1].rsplit("_", 1)[0]
            rel = Path(*parts[2:])
            sources.append(ROOT / "units" / core_dir / rel)
    toplevel = edam.get("toplevel", CORE_NAME)
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return sources, toplevel


@pytest.fixture(scope="module")
def design():
    edam = _fusesoc_edam()
    sources, toplevel = _sources_from_edam(edam)
    assert sources, f"no HDL sources resolved for {CORE_NAME}"
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
def test_stream_sink(design, cocotb_testcase):
    _run(design, cocotb_testcase)
