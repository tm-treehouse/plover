#!/usr/bin/env python3
"""
FuseSoC generator: regenerate register artifacts from the RDL at build time.

FuseSoC invokes this with a single argument — a YAML config file it writes,
containing at least:
    gapi: '1.0'
    files_root: <abs path to the calling core's root>
    vlnv: <generated core VLNV>
    parameters: { ... any params passed from the generate section ... }

This wrapper runs ``rdl/gen_regs.py`` (the real generator) against the RDL in
files_root, then writes a ``<name>.core`` next to its outputs describing the
generated HTML docs + C header so they're tracked by FuseSoC. The Python
``regmap.py`` is emitted straight into the dv/ package (it's consumed by
pytest, not by the EDA backend) and is intentionally not listed as a core
fileset.
"""
from __future__ import annotations

import os
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

    rdl = files_root / params.get("rdl_file", "rdl/axil_shell.rdl")
    gen_script = files_root / "rdl" / "gen_regs.py"
    outdir = Path("gen").resolve()          # generator runs in its own out dir
    regmap_out = files_root / "dv" / "axil_shell" / "regmap.py"

    # Run the real generator. regmap.py goes into the package; docs+header go
    # into the generator's output dir (picked up by the emitted core below).
    subprocess.run(
        [sys.executable, str(gen_script),
         "--rdl", str(rdl),
         "--outdir", str(outdir),
         "--regmap-out", str(regmap_out)],
        check=True,
    )

    # Emit a core describing the generated doc/header artifacts.
    vlnv = cfg.get("vlnv", "::axil_shell_rdl_gen:0")
    core = {
        "name": vlnv,
        "filesets": {
            "rdl_generated": {
                "files": [
                    {"gen/axil_shell_regs.h": {"file_type": "cSource",
                                               "is_include_file": True}},
                ],
            },
        },
        "targets": {"default": {"filesets": ["rdl_generated"]}},
    }
    out_core = Path(f"{vlnv.split(':')[2]}.core")
    out_core.write_text("CAPI=2:\n" + yaml.safe_dump(core, sort_keys=False))
    print(f"[rdl_gen] generated regmap + docs from {rdl.name}; wrote {out_core}")


if __name__ == "__main__":
    main()
