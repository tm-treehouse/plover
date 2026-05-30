#!/usr/bin/env python3
"""
Shared FuseSoC generator wrapper: regenerate register artifacts from RDL.

Invoked by any unit's ``rdl_gen`` generator slot via:

    generators:
      rdl_gen:
        command: ../../tools/rdl_gen.py
        interpreter: python3

FuseSoC writes a YAML config and passes its path as argv[1]. Recognized
parameters (from the calling core's ``generate:`` block):

    rdl_file           — RDL source path, relative to the core root.
    regmap_out         — Python regmap output path, relative to the core
                         root. (consumed by the unit's testbench)
    gen_script         — path to gen_regs.py, relative to the core root.
                         Defaults to the ``gen_regs.py`` next to this
                         wrapper (i.e. ``tools/gen_regs.py``).
    c_header_basename  — basename of the generated C header (without .h).
                         Defaults to the unit name extracted from the VLNV.
    docs_out           — generator-relative output dir for HTML + C header.
                         Defaults to ``gen`` (created next to the .eda.yml).

Most units use the shared ``tools/gen_regs.py`` (the real generator). A
unit can override via the ``gen_script`` parameter if it needs custom
emission logic — the wrapper here only handles the FuseSoC-side plumbing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: rdl_gen.py <fusesoc_config.yml>")

    cfg = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
    files_root = Path(cfg.get("files_root", ".")).resolve()
    params = cfg.get("parameters") or {}
    vlnv = cfg.get("vlnv", "::unknown_rdl_gen:0")
    unit_name = vlnv.split(":")[2].rsplit("_", 1)[0] \
        if vlnv.count(":") >= 2 else "unknown"

    rdl_file = params.get("rdl_file")
    if not rdl_file:
        sys.exit("rdl_gen.py: 'rdl_file' parameter required")
    rdl = files_root / rdl_file

    gen_script = params.get("gen_script")
    if gen_script:
        gen_script = files_root / gen_script
    else:
        # Default: shared tools/gen_regs.py next to this wrapper.
        gen_script = Path(__file__).resolve().parent / "gen_regs.py"
    regmap_out = files_root / params.get("regmap_out", "dv/regmap.py")
    c_header_basename = params.get("c_header_basename")
    if not c_header_basename:
        sys.exit("rdl_gen.py: 'c_header_basename' parameter required "
                 "(e.g. 'axil_shell_regs')")
    # C++ header options. Defaults are usually fine.
    cpp_header_basename = params.get("cpp_header_basename", c_header_basename)
    cpp_namespace = params.get("cpp_namespace", "plover_regs")
    cpp_class_name = params.get("cpp_class_name")  # None -> peakrdl picks addrmap name
    outdir = Path(params.get("docs_out", "gen")).resolve()

    cmd = [sys.executable, str(gen_script),
           "--rdl", str(rdl),
           "--outdir", str(outdir),
           "--regmap-out", str(regmap_out),
           "--c-header-name", c_header_basename,
           "--cpp-header-name", cpp_header_basename,
           "--cpp-namespace", cpp_namespace]
    if cpp_class_name:
        cmd += ["--cpp-class-name", cpp_class_name]
    subprocess.run(cmd, check=True)

    # Emit a core describing the generated C and C++ headers. (HTML docs
    # aren't a synthesis/sim input, so they're not listed here.) Both headers
    # are tagged as include files so FuseSoC's source-resolution picks up
    # their containing directory for -I purposes.
    core = {
        "name": vlnv,
        "filesets": {
            "rdl_generated": {
                "files": [
                    {f"{outdir.name}/{c_header_basename}.h": {
                        "file_type": "cSource",
                        "is_include_file": True,
                    }},
                    {f"{outdir.name}/{cpp_header_basename}.hh": {
                        "file_type": "cppSource",
                        "is_include_file": True,
                    }},
                ],
            },
        },
        "targets": {"default": {"filesets": ["rdl_generated"]}},
    }
    out_core = Path(f"{vlnv.split(':')[2]}.core")
    out_core.write_text("CAPI=2:\n" + yaml.safe_dump(core, sort_keys=False))
    print(f"[rdl_gen] {unit_name}: regenerated regmap + docs from {rdl.name}")


if __name__ == "__main__":
    main()
