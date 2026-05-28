"""
FuseSoC + pytest harness for the AXI-Lite shell.

Design split:
* **FuseSoC** owns the IP/source metadata (``axil_shell.core``). We ask FuseSoC
  to resolve the ``default`` target into an EDAM file (its machine-readable
  build manifest) and read the source list + toplevel + parameters from it.
* **pytest** owns test orchestration. Each pytest test builds the resolved RTL
  with the newest Verilator via cocotb's runner API and runs one pyuvm test.

This keeps a single source of truth for "what RTL is in this design" (the
.core file) and lets pytest parametrize / select tests the way Python users
expect — no Makefile.

Run:
    pytest                         # all tests on Verilator
    pytest -k smoke                # just the smoke test
    SIM=icarus pytest              # fall back to Icarus
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent            # units/axil_shell/dv
UNIT_DIR = HERE.parent                            # units/axil_shell
ROOT = UNIT_DIR.parents[1]                        # repo root (has fusesoc.conf)
CORE_NAME = "axil_shell"
# Target used purely to resolve the source list into an EDAM. It must have a
# tool so FuseSoC can set up a flow; the verilator "lint" target lists the same
# rtl fileset and is the natural choice. We only consume its file list — the
# actual build/run is done by cocotb's runner below.
RESOLVE_TARGET = "lint"

# Per-simulator Verilator/Icarus build args.
BUILD_ARGS = {
    "verilator": ["--trace", "--trace-structs",
                  "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
                  "-Wno-WIDTHEXPAND"],
    "icarus": ["-g2012"],
}


def _resolve_verilator_root() -> None:
    """Point VERILATOR_ROOT/PATH at the pip ``verilator`` wheel (newest
    release) so cocotb and FuseSoC both use it rather than any older system
    package.
    """
    try:
        import verilator  # type: ignore
    except Exception:
        return
    pkg = Path(verilator.__file__).resolve().parent
    if (pkg / "bin" / "verilator").exists():
        os.environ["VERILATOR_ROOT"] = str(pkg)
        os.environ["PATH"] = f"{pkg / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"


def _fusesoc_edam() -> dict:
    """Run ``fusesoc run --setup`` to resolve the source list and return the
    parsed EDAM manifest (source list, toplevel, parameters).
    """
    # --setup stops after generating the EDAM/Makefile; no build/run.
    subprocess.run(
        ["fusesoc", "run", "--target", RESOLVE_TARGET, "--setup", CORE_NAME],
        cwd=ROOT, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # FuseSoC writes build/<name>/<target>-<tool>/<name>.eda.yml. With no
    # default_tool on the default target it still emits an EDAM under the
    # target dir; find the newest one.
    candidates = sorted(
        (ROOT / "build").rglob(f"{CORE_NAME}_*/*/*.eda.yml"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        pytest.skip("FuseSoC did not produce an EDAM file; is fusesoc installed?")
    return yaml.safe_load(candidates[-1].read_text())


def _sources_from_edam(edam: dict) -> tuple[list[Path], str]:
    """Extract HDL source paths and the toplevel from an EDAM manifest.

    EDAM file paths look like ``src/<core>_<ver>/rtl/<file>`` — FuseSoC stages
    copies under its build dir, stripping the core's root path. We resolve
    them back to the live RTL under the unit's own directory so rebuilds
    always reflect edits.
    """
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
            sources.append(UNIT_DIR / rel)
    toplevel = edam.get("toplevel", "axil_shell")
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return sources, toplevel


@pytest.fixture(scope="session")
def design():
    """Resolve the design once per session via FuseSoC."""
    edam = _fusesoc_edam()
    sources, toplevel = _sources_from_edam(edam)
    assert sources, "no HDL sources resolved from FuseSoC EDAM"
    return {"sources": sources, "toplevel": toplevel}


def _run(design, cocotb_testcase: str) -> None:
    """Build the resolved RTL and run a single cocotb/pyuvm test."""
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        _resolve_verilator_root()

    # Waveform dump is off by default (keeps runs fast). Enable with WAVES=1
    # (or `pytest --waves`, see conftest.py). The RTL is already compiled with
    # tracing, so this just turns the runtime dump on.
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
        test_module="test_axil_shell",
        test_dir=str(HERE),
        testcase=cocotb_testcase,
        waves=waves,
        extra_env={"PYTHONPATH": f"{HERE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"},
    )


# Each cocotb test (the @pyuvm.test() classes in test_axil_shell.py) gets its
# own pytest test, so `pytest -k smoke` / `-k sweep` select them and failures
# are reported per-test. cocotb writes results.xml; a nonzero failure count
# raises SystemExit from runner.test, which pytest reports as a failure.
@pytest.mark.parametrize("cocotb_testcase", ["smoke", "sweep"])
def test_axil_shell(design, cocotb_testcase):
    _run(design, cocotb_testcase)
