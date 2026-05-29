#!/usr/bin/env python3
"""
Shared FuseSoC generator wrapper: emit a SystemVerilog version header from git.

Invoked by any unit's ``version_gen`` slot via:

    generators:
      version_gen:
        command: ../../tools/version_gen.py
        interpreter: python3

Recognized parameters (from the calling core's ``generate:`` block):

    gen_script  — path to the unit's gen_version.py, relative to the core
                  root. Defaults to ``rdl/gen_version.py``.
    out         — output header path, relative to the generator's run dir
                  (i.e. the build dir). Defaults to the unit name +
                  ``_version_pkg.svh``.
    repo        — git repo to read; defaults to the core root.

The output header is listed in the emitted core's fileset so FuseSoC
includes it in the build.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: version_gen.py <fusesoc_config.yml>")

    cfg = yaml.safe_load(Path(sys.argv[1]).read_text()) or {}
    files_root = Path(cfg.get("files_root", ".")).resolve()
    params = cfg.get("parameters") or {}
    vlnv = cfg.get("vlnv", "::unknown_version_gen:0")
    unit_token = vlnv.split(":")[2] if vlnv.count(":") >= 2 else "unknown"
    unit_name = unit_token.removesuffix("_version_gen") if hasattr(str, "removesuffix") \
        else (unit_token[:-len("_version_gen")] if unit_token.endswith("_version_gen")
              else unit_token)

    gen_script = files_root / params.get("gen_script", "rdl/gen_version.py")
    out_name = params.get("out", f"{unit_name}_version_pkg.svh")
    out_path = Path(out_name).resolve()                # under the run dir
    repo = Path(params.get("repo", str(files_root))).resolve()

    subprocess.run(
        [sys.executable, str(gen_script), "--repo", str(repo), "--out", str(out_path)],
        check=True,
    )

    core = {
        "name": vlnv,
        "filesets": {
            "version_generated": {
                "files": [
                    {out_name: {"file_type": "systemVerilogSource",
                                "is_include_file": True}},
                ],
            },
        },
        "targets": {"default": {"filesets": ["version_generated"]}},
    }
    out_core = Path(f"{unit_token}.core")
    out_core.write_text("CAPI=2:\n" + yaml.safe_dump(core, sort_keys=False))
    print(f"[version_gen] {unit_name}: wrote {out_name}")


if __name__ == "__main__":
    main()
