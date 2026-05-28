# `plover/syn/` — synthesis scaffolding

This subtree is intentionally vendor-agnostic at this stage; it lays down
the **directory pattern** a real synthesis flow will drop into, not an
operational flow.

```
syn/
  constraints/
    plover.sdc        timing constraints (SDC stub — 100 MHz placeholder)
  scripts/
    build.sh          driver stub; prints next-step instructions
```

## What's missing on purpose

* **Pin constraints** (XDC / QSF / PCF / LPF) — board-specific.
* **Tool invocation** — once a vendor is picked, this becomes a
  `default_tool` + `tools:` block on the `syn` target in
  `plover/plover.core`, and `fusesoc run --target=syn plover` drives it
  through Edalize. The shell script here is a placeholder for the
  pre-FuseSoC alternative if needed.
* **IP / vendor primitives** — none in plover.sv today (the design is
  inferable RTL), but if you add e.g. clock-management blocks they live
  under `syn/ip/` or behind synthesis-only `ifdef`s in the RTL with sim
  stubs.

## When you pick a vendor

The minimum to make `syn` operational:

1. Add a constraints fileset to `plover/plover.core` and depend on it from
   the `syn` target.
2. Set `default_tool` to the vendor (e.g. `vivado`, `quartus`, `icestorm`)
   and add the matching `tools:` block.
3. Add board pin constraints alongside `plover.sdc` in `constraints/`.
4. `fusesoc run --target=syn plover` — Edalize handles the rest.
