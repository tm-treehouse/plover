#!/usr/bin/env python3
"""
Generate register artifacts from a SystemRDL map.

Shared across every unit that has an ``.rdl`` (today: axil_shell, syscon).
The unit's ``.rdl`` is the single source of truth for that block's
register layout; this script consumes it and produces, under the unit's
build dir (git-ignored, regenerated at build):

* ``regmap.py``         — a small, dependency-free Python register map
                          (plain dataclasses): per-register address +
                          per-field position, mask, reset, access. The
                          testbench imports this so sequences and the
                          scoreboard reference registers by name instead
                          of magic numbers. Usable elsewhere too
                          (software models, other blocks) since it has
                          no PeakRDL runtime dependency.
* ``html/``             — browsable HTML documentation (peakrdl-html).
* ``<addrmap>_regs.h``  — C header for firmware (peakrdl-cheader). The
                          basename defaults to the RDL filename (e.g.
                          ``axil_shell_regs.h``) but is overridable.

Run directly (per unit):
    python tools/gen_regs.py --rdl units/<unit>/rdl/<unit>.rdl \\
                             --outdir <unit>/build/gen

It is also invoked by the FuseSoC generator (``tools/rdl_gen.py``) at
build time, so the generated artifacts always match the live RDL.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from systemrdl import RDLCompiler
from systemrdl.node import FieldNode, RegNode


HEADER = '''"""AUTO-GENERATED from {rdl}. Do not edit by hand.

Dependency-free static register map for the AXI-Lite shell. Regenerate with
``python rdl/gen_regs.py`` or via the FuseSoC generator.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    lsb: int
    msb: int
    width: int
    mask: int          # mask within the register word
    reset: int
    sw_readable: bool
    sw_writable: bool


@dataclass(frozen=True)
class Register:
    name: str
    offset: int        # byte offset within the addrmap
    width: int         # bits
    fields: dict[str, Field]

    def field(self, name: str) -> Field:
        return self.fields[name]


'''


def _access_flags(field: FieldNode) -> tuple[bool, bool]:
    return bool(field.is_sw_readable), bool(field.is_sw_writable)


def generate_regmap(rdl_path: Path, out_path: Path) -> None:
    rdlc = RDLCompiler()
    rdlc.compile_file(str(rdl_path))
    root = rdlc.elaborate()

    # The top addrmap is the only addrmap child of root.
    top = next(c for c in root.children() if c.inst_name)

    lines: list[str] = [HEADER.format(rdl=rdl_path.as_posix())]
    reg_entries: list[str] = []

    regs = [n for n in top.descendants() if isinstance(n, RegNode)]
    for reg in regs:
        fld_lines = []
        for f in reg.fields():
            lsb, msb, width = f.lsb, f.msb, f.width
            mask = ((1 << width) - 1) << lsb
            reset = f.get_property("reset")
            reset = 0 if reset is None else int(reset)
            sw_r, sw_w = _access_flags(f)
            fld_lines.append(
                f'        "{f.inst_name}": Field("{f.inst_name}", '
                f'{lsb}, {msb}, {width}, 0x{mask:08X}, 0x{reset:X}, '
                f'{sw_r}, {sw_w}),'
            )
        offset = reg.address_offset
        width = reg.get_property("regwidth")
        reg_entries.append(reg.inst_name)
        lines.append(
            f'{reg.inst_name} = Register(\n'
            f'    name="{reg.inst_name}",\n'
            f'    offset=0x{offset:02X},\n'
            f'    width={width},\n'
            f'    fields={{\n' + "\n".join(fld_lines) + "\n    },\n)\n"
        )

    # A dict + name->offset convenience map.
    lines.append("\nREGISTERS = {\n" +
                 "\n".join(f'    "{r}": {r},' for r in reg_entries) +
                 "\n}\n")
    lines.append("\nOFFSETS = {name: reg.offset for name, reg in REGISTERS.items()}\n")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    print(f"[gen_regs] wrote {out_path} ({len(regs)} registers)")


def run_peakrdl(subcmd: list[str], desc: str) -> None:
    try:
        subprocess.run(["peakrdl", *subcmd], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(f"[gen_regs] {desc}: ok")
    except FileNotFoundError:
        print(f"[gen_regs] {desc}: SKIPPED (peakrdl not installed)", file=sys.stderr)
    except subprocess.CalledProcessError as e:
        out = e.stdout.decode() if e.stdout else ""
        print(f"[gen_regs] {desc}: FAILED\n{out}", file=sys.stderr)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate register artifacts from RDL")
    ap.add_argument("--rdl", required=True, type=Path)
    ap.add_argument("--outdir", default="gen", type=Path,
                    help="output dir for docs + C header")
    ap.add_argument("--regmap-out", default="dv/regmap.py", type=Path,
                    help="path for the generated Python regmap consumed by the TB")
    ap.add_argument("--c-header-name", default=None,
                    help="basename (without .h) for the C header; "
                         "defaults to '<addrmap>_regs'")
    ap.add_argument("--no-docs", action="store_true",
                    help="skip HTML + C header (only emit regmap.py)")
    args = ap.parse_args()

    rdl = args.rdl.resolve()
    outdir = args.outdir.resolve()

    generate_regmap(rdl, args.regmap_out.resolve())

    if not args.no_docs:
        # Default C-header basename derives from the RDL filename so the
        # output is predictable per unit (e.g. syscon.rdl -> syscon_regs.h).
        c_basename = args.c_header_name or f"{rdl.stem}_regs"
        run_peakrdl(["html", str(rdl), "-o", str(outdir / "html")], "HTML docs")
        run_peakrdl(["c-header", str(rdl), "-o", str(outdir / f"{c_basename}.h")],
                    "C header")


if __name__ == "__main__":
    main()
