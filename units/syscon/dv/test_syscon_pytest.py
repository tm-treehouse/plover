"""FuseSoC + pytest harness for the syscon unit. Same shape as the other
units, except this one passes ``parameters=`` to the Verilator build so the
VERSION/VERSION_HASH registers have deterministic values for scoreboard
comparison (matching the EXPECTED_* constants in test_syscon.py).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

CORE_NAME = "syscon"
TEST_MODULE = "test_syscon"
TESTCASES = ["smoke", "reset_cause"]
RESOLVE_TARGET = "lint"

HERE = Path(__file__).resolve().parent
UNIT_DIR = HERE.parent
ROOT = UNIT_DIR.parents[1]

# Must match EXPECTED_VERSION / EXPECTED_VERSION_HASH in test_syscon.py.
VERSION_OVERRIDE      = 0xCAFE_F00D
VERSION_HASH_OVERRIDE = 0x1234_5678

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


def _sources_from_edam(edam: dict) -> tuple[list[Path], str, list[Path]]:
    """Extract HDL source paths, toplevel, and include dirs from an EDAM manifest.

    Generated SystemVerilog headers (``is_include_file: true``) are not
    passed as sources — they're consumed via the ``\\`include`` directive.
    Their directories are returned separately so the harness can pass them
    to Verilator as ``-I`` paths.
    """
    hdl_types = {"verilogSource", "systemVerilogSource"}
    sources: list[Path] = []
    include_dirs: set[Path] = set()
    candidates = sorted(
        (ROOT / "build").rglob(f"{CORE_NAME}_*/*/*.eda.yml"),
        key=lambda p: p.stat().st_mtime,
    )
    eda_dir = candidates[-1].parent

    for f in edam.get("files", []):
        if f.get("file_type") not in hdl_types:
            continue
        name = f["name"]
        # Headers: record their containing directory and move on.
        if f.get("is_include_file"):
            include_dirs.add((eda_dir / name).parent.resolve())
            continue
        parts = Path(name).parts
        if parts and parts[0] == "src" and len(parts) >= 3:
            core_dir = parts[1].rsplit("_", 1)[0]
            rel = Path(*parts[2:])
            sources.append(ROOT / "units" / core_dir / rel)
        else:
            sources.append(eda_dir / name)
    toplevel = edam.get("toplevel", CORE_NAME)
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return sources, toplevel, sorted(include_dirs)


@pytest.fixture(scope="module")
def design():
    edam = _fusesoc_edam()
    sources, toplevel, include_dirs = _sources_from_edam(edam)
    assert sources, f"no HDL sources resolved from FuseSoC EDAM for {CORE_NAME}"
    return {"sources": sources, "toplevel": toplevel,
            "include_dirs": include_dirs}


def _run(design, cocotb_testcase: str) -> None:
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        _resolve_verilator_root()
    waves = os.getenv("WAVES", "0") not in ("0", "", "false", "False")

    extra_build = [f"-I{d}" for d in design["include_dirs"]]

    runner = get_runner(sim)
    runner.build(
        sources=design["sources"],
        hdl_toplevel=design["toplevel"],
        build_args=BUILD_ARGS.get(sim, []) + extra_build,
        parameters={
            "VERSION_OVERRIDE": VERSION_OVERRIDE,
            "VERSION_HASH_OVERRIDE": VERSION_HASH_OVERRIDE,
        },
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
def test_syscon(design, cocotb_testcase):
    _run(design, cocotb_testcase)
