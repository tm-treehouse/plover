"""
FuseSoC + pytest harness for the plover project-top testbench.

The ``plover`` core depends on ``axil_shell``, ``counter``, and ``syscon``,
so FuseSoC's EDAM resolves the full source list (top integration + all
three sub-units) and the cocotb runner builds the whole hierarchy.

Two non-trivial bits beyond the unit harnesses:

1. **Generated include files.** ``syscon`` ships with a build-time generated
   ``syscon_version_pkg.svh`` (via the version_gen FuseSoC generator). The
   EDAM lists it as ``is_include_file: true``; we collect its containing
   directory and pass ``-I<dir>`` to Verilator so the include resolves.

2. **Version parameters.** We pass deterministic ``VERSION_OVERRIDE`` /
   ``VERSION_HASH_OVERRIDE`` to the syscon instance at build so the test
   has known values to compare against, regardless of git state. These
   match the EXPECTED_* constants in ``test_plover.py``.
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
TESTCASES = ["smoke", "firmware_smoke", "firmware_concurrent"]
RESOLVE_TARGET = "lint"

HERE = Path(__file__).resolve().parent          # top/dv
PROJ_DIR = HERE.parent                          # top/
ROOT = PROJ_DIR.parent                          # repo root

# Must match EXPECTED_* in test_plover.py.
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


def _fusesoc_edam() -> tuple[dict, Path]:
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
    eda_yml = candidates[-1]
    return yaml.safe_load(eda_yml.read_text()), eda_yml.parent


def _sources_from_edam(edam: dict, eda_dir: Path) -> tuple[list[Path], str, list[Path]]:
    """Resolve EDAM entries to (live RTL sources, toplevel, include dirs).

    RTL files (``src/<core>_<ver>/rtl/<file>``) are back-mapped to their
    live unit dir so edits to RTL are picked up by rebuilds without going
    through FuseSoC's staging.

    Generated include files (``is_include_file: true``) live only in the
    build dir — they have no live source — so we resolve them to absolute
    paths in the build dir and return their directories for ``-I`` use.

    Returns four-tuple: (sources, toplevel, hdl_include_dirs, c_include_dirs).
    HDL include dirs go to Verilator; C include dirs (peakrdl-cheader ``.h`` output)
    go to the firmware compile via PLOVER_RDL_INCLUDE_DIRS.
    """
    hdl_types = {"verilogSource", "systemVerilogSource"}
    c_types = {"cSource"}
    sources: list[Path] = []
    hdl_include_dirs: set[Path] = set()
    c_include_dirs: set[Path] = set()

    for f in edam.get("files", []):
        ftype = f.get("file_type")
        name = f["name"]
        if ftype in c_types and f.get("is_include_file"):
            # peakrdl-cheader generated headers — for the firmware compile only.
            c_include_dirs.add((eda_dir / name).parent.resolve())
            continue
        if ftype not in hdl_types:
            continue
        if f.get("is_include_file"):
            hdl_include_dirs.add((eda_dir / name).parent.resolve())
            continue
        parts = Path(name).parts
        if "rtl" not in parts:
            continue
        rtl_idx = parts.index("rtl")
        staged_core = parts[rtl_idx - 1]
        rel = Path(*parts[rtl_idx:])
        core_name = staged_core.rsplit("_", 1)[0]
        live_dir = PROJ_DIR if core_name == CORE_NAME else ROOT / "units" / core_name
        sources.append(live_dir / rel)

    toplevel = edam.get("toplevel", CORE_NAME)
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return sources, toplevel, sorted(hdl_include_dirs), sorted(c_include_dirs)


@pytest.fixture(scope="module")
def design():
    edam, eda_dir = _fusesoc_edam()
    sources, toplevel, hdl_includes, c_includes = _sources_from_edam(edam, eda_dir)
    assert sources, f"no HDL sources resolved from FuseSoC EDAM for {CORE_NAME}"
    return {"sources": sources, "toplevel": toplevel,
            "hdl_include_dirs": hdl_includes,
            "c_include_dirs": c_includes}


def _run(design, cocotb_testcase: str) -> None:
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        _resolve_verilator_root()
    waves = os.getenv("WAVES", "0") not in ("0", "", "false", "False")

    extra_build = [f"-I{d}" for d in design["hdl_include_dirs"]]

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
        extra_env={
            "PYTHONPATH": f"{HERE}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            # The C++ firmware test (firmware_smoke) reads this to know
            # where the peakrdl-cheader generated headers live for its .so
            # .so build. os.pathsep-separated so the cocotb test can split it.
            "PLOVER_RDL_INCLUDE_DIRS": os.pathsep.join(
                str(d) for d in design["c_include_dirs"]),
        },
    )


@pytest.mark.parametrize("cocotb_testcase", TESTCASES)
def test_plover(design, cocotb_testcase):
    _run(design, cocotb_testcase)
