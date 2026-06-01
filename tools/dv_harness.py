"""Shared FuseSoC + pytest harness for plover unit testbenches.

Every unit's ``test_<unit>_pytest.py`` is a thin shim that calls
:func:`run_testcase` with a :class:`HarnessConfig`. The shared module
owns:

* Verilator-from-pip-wheel resolution.
* ``fusesoc run --setup`` and EDAM parsing.
* Translation of EDAM file lists back to live RTL paths (so edits
  rebuild without going through FuseSoC's stage step).
* Build-arg defaults for Verilator/Icarus.
* ``cocotb_tools.runner`` build + test invocation.

The per-unit shim controls only what actually differs between units:
the core name, toplevel module, testcase list, optional build
parameters, optional extra DV-only SV sources, and whether to thread
C/C++ generated-header dirs through to the runtime env (the top needs
this for firmware compilation).

Design rationale: before this consolidation, six harnesses had each
copied the same boilerplate and then drifted in small ways (different
source-resolution branches, different include-dir handling, different
build-arg lists). Centralising it removes the drift potential and means
adding a new unit is a tiny shim, not a copy of the closest-looking
existing file.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pytest
import yaml

# Cocotb runner import is lazy — keeps `python -c "import tools.dv_harness"`
# fast and avoids pulling cocotb when someone is just inspecting the file.
def _get_runner(sim: str):
    from cocotb_tools.runner import get_runner
    return get_runner(sim)


# ---- Defaults --------------------------------------------------------

# Default per-simulator build args. The Verilator list is the lowest
# common denominator that's been adequate for every unit so far; specific
# units can override via HarnessConfig.extra_build_args.
DEFAULT_BUILD_ARGS: Mapping[str, Sequence[str]] = {
    "verilator": [
        "--trace", "--trace-structs",
        "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL",
        "-Wno-WIDTHEXPAND",
    ],
    "icarus": ["-g2012"],
}

# Source-resolution target: every unit core has a 'lint' target with the
# same rtl fileset and a default_tool, so this is always a safe pick.
DEFAULT_RESOLVE_TARGET = "lint"


# ---- Config object ---------------------------------------------------

@dataclass
class HarnessConfig:
    """Per-unit knobs that drive the shared harness.

    Required:
        core_name        FuseSoC VLNV name (e.g. "axil_shell", "plover").
        test_module      Python module containing the cocotb @pyuvm.test()
                          classes (e.g. "test_axil_shell").
        here             Path to the unit's dv/ directory (the harness uses
                          this as the cocotb test_dir and PYTHONPATH entry).
        root             Path to the repo root.

    Optional:
        hdl_toplevel     If None, uses core_name. Override for units whose
                          DV wraps a different toplevel (e.g. axil_xbar's
                          DV uses "axil_xbar_dv_top").
        extra_sources    Extra SV files outside the FuseSoC RTL fileset
                          (e.g. axil_xbar's RAM stub + DV-top wrapper).
        parameters       Verilator -G parameters passed at build time
                          (e.g. syscon's VERSION_OVERRIDE).
        extra_build_args Per-simulator extra args appended to defaults.
        c_include_env    If non-empty, the harness collects C include dirs
                          from the EDAM and exports them to the cocotb env
                          under this variable name (e.g. plover top uses
                          "PLOVER_RDL_INCLUDE_DIRS").
        live_dir_map     Map from FuseSoC core_name prefix -> live dir.
                          Defaults to ROOT/units/<core>/. Override only
                          for the project top (where one prefix maps to
                          ROOT/top instead of ROOT/units/...).
    """
    core_name: str
    test_module: str
    here: Path
    root: Path
    hdl_toplevel: Optional[str] = None
    extra_sources: Sequence[Path] = field(default_factory=list)
    parameters: Optional[Mapping[str, Any]] = None
    extra_build_args: Mapping[str, Sequence[str]] = field(default_factory=dict)
    c_include_env: Optional[str] = None
    live_dir_map: Optional[Mapping[str, Path]] = None


# ---- Verilator resolution -------------------------------------------

def resolve_verilator_root() -> None:
    """Point VERILATOR_ROOT/PATH at the pip ``verilator`` wheel.

    Without this, cocotb and FuseSoC may pick up an older system Verilator
    that doesn't support what the testbenches need (e.g. newer warnings).
    Safe to call multiple times; no-op when there's no verilator wheel.
    """
    try:
        import verilator  # type: ignore
    except Exception:
        return
    pkg = Path(verilator.__file__).resolve().parent
    if (pkg / "bin" / "verilator").exists():
        os.environ["VERILATOR_ROOT"] = str(pkg)
        os.environ["PATH"] = f"{pkg / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"


# ---- FuseSoC EDAM ---------------------------------------------------

def fusesoc_edam(cfg: HarnessConfig,
                 target: str = DEFAULT_RESOLVE_TARGET) -> tuple[dict, Path]:
    """Resolve the design via FuseSoC --setup and return (edam, eda_dir).

    ``eda_dir`` is the directory containing the EDAM (the FuseSoC staging
    dir for the target+tool). Generated headers under
    ``<eda_dir>/src/<core>_<ver>/gen/`` are referenced relative to it.
    """
    subprocess.run(
        ["fusesoc", "run", "--target", target, "--setup", cfg.core_name],
        cwd=cfg.root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    candidates = sorted(
        (cfg.root / "build").rglob(f"{cfg.core_name}_*/*/*.eda.yml"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        pytest.skip(
            f"FuseSoC did not produce an EDAM file for {cfg.core_name}; "
            "is fusesoc installed?"
        )
    edam_path = candidates[-1]
    return yaml.safe_load(edam_path.read_text()), edam_path.parent


# ---- EDAM -> sources / include dirs ---------------------------------

def _live_dir_for(cfg: HarnessConfig, core_name: str) -> Path:
    """Map a staged-core name back to its live source dir.

    Default: ``units/<core>/``. Overridable per-unit via HarnessConfig.
    """
    if cfg.live_dir_map and core_name in cfg.live_dir_map:
        return cfg.live_dir_map[core_name]
    return cfg.root / "units" / core_name


def sources_from_edam(cfg: HarnessConfig, edam: dict, eda_dir: Path
                      ) -> tuple[list[Path], str, list[Path], list[Path]]:
    """Extract (sources, toplevel, hdl_includes, c_includes) from an EDAM.

    Sources: HDL files mapped back from ``src/<core>_<ver>/rtl/<file>``
    to the live unit dir, so RTL edits rebuild without re-running FuseSoC.

    HDL includes: generator-emitted SV include files (any
    ``is_include_file: true`` of HDL type), resolved to absolute paths
    in the build dir. Returned as a list of directories for ``-I``.

    C includes: ``cSource``-typed include files (peakrdl-cheader output).
    The caller decides what to do with these; the top harness threads
    them through to the firmware compile via an env var.

    Toplevel: from EDAM's ``toplevel`` key; if a list, takes the first.
    The caller can override via HarnessConfig.hdl_toplevel.
    """
    hdl_types = {"verilogSource", "systemVerilogSource"}
    sources: list[Path] = []
    hdl_includes: set[Path] = set()
    c_includes: set[Path] = set()

    for f in edam.get("files", []):
        ftype = f.get("file_type")
        name = f["name"]
        is_inc = bool(f.get("is_include_file"))

        if ftype == "cSource" and is_inc:
            c_includes.add((eda_dir / name).parent.resolve())
            continue

        if ftype not in hdl_types:
            continue

        if is_inc:
            hdl_includes.add((eda_dir / name).parent.resolve())
            continue

        parts = Path(name).parts
        if "rtl" not in parts:
            continue
        rtl_idx = parts.index("rtl")
        staged_core = parts[rtl_idx - 1]
        rel = Path(*parts[rtl_idx:])
        core_name = staged_core.rsplit("_", 1)[0]
        sources.append(_live_dir_for(cfg, core_name) / rel)

    toplevel = edam.get("toplevel", cfg.core_name)
    if isinstance(toplevel, list):
        toplevel = toplevel[0]
    return (sources, toplevel,
            sorted(hdl_includes), sorted(c_includes))


# ---- Build + run -----------------------------------------------------

def run_testcase(cfg: HarnessConfig, cocotb_testcase: str,
                 parameters: Optional[Mapping[str, Any]] = None) -> None:
    """Build the design and run one cocotb testcase.

    ``parameters`` override the cfg.parameters defaults (used by harnesses
    that sweep build-time parameters, e.g. axil_xbar's stage configs).
    """
    sim = os.getenv("SIM", "verilator")
    if sim == "verilator":
        resolve_verilator_root()
    waves = os.getenv("WAVES", "0") not in ("0", "", "false", "False")

    edam, eda_dir = fusesoc_edam(cfg)
    sources, toplevel, hdl_includes, c_includes = sources_from_edam(cfg, edam, eda_dir)
    sources = list(sources) + list(cfg.extra_sources)
    if cfg.hdl_toplevel is not None:
        toplevel = cfg.hdl_toplevel

    build_args = list(DEFAULT_BUILD_ARGS.get(sim, []))
    build_args += list(cfg.extra_build_args.get(sim, []))
    build_args += [f"-I{d}" for d in hdl_includes]

    effective_params = dict(cfg.parameters or {})
    if parameters:
        effective_params.update(parameters)

    extra_env: dict[str, str] = {
        "PYTHONPATH": os.pathsep.join([
            str(cfg.here), str(cfg.root), os.environ.get("PYTHONPATH", "")]),
    }
    if cfg.c_include_env:
        extra_env[cfg.c_include_env] = os.pathsep.join(str(d) for d in c_includes)

    runner = _get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel=toplevel,
        build_args=build_args,
        parameters=effective_params,
        waves=waves,
        always=True,
    )
    runner.test(
        hdl_toplevel=toplevel,
        test_module=cfg.test_module,
        test_dir=str(cfg.here),
        testcase=cocotb_testcase,
        waves=waves,
        extra_env=extra_env,
    )
