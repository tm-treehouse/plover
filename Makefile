# Convenience wrappers around the real flows. These are shortcuts only — the
# test flow is `uv run pytest` (see README), and FuseSoC owns the build.
# Nothing here is required.
#
#   make sync      create/update the environment from uv.lock
#   make test      run the cocotb/pyuvm tests
#   make waves     run the tests with waveform dumping enabled
#   make smoke     run just the smoke test
#   make sweep     run just the sweep test
#   make lint      Verilator lint via FuseSoC
#   make regs      regenerate the register map + docs from the RDL
#   make docs      regenerate then print the path to the HTML register docs
#   make clean     remove build/sim/generated artifacts
#   make distclean make clean + remove the uv environment

# uv manages the environment; `uv run` ensures the lockfile + venv are in sync
# before each command, and the verilator wheel (newest release) lives inside
# that venv, so no VERILATOR_ROOT wrangling is needed.
UV  ?= uv
RUN ?= $(UV) run

.PHONY: sync test smoke sweep lint regs docs clean distclean help waves

help:
	@grep -E '^#   make' $(MAKEFILE_LIST) | sed 's/^#   /  /'

sync:
	$(UV) sync

test:
	$(RUN) pytest -v

waves:
	$(RUN) pytest -v --waves

smoke:
	$(RUN) pytest -v -k smoke

sweep:
	$(RUN) pytest -v -k sweep

lint:
	$(RUN) fusesoc run --target=lint axil_shell

regs:
	$(RUN) python units/axil_shell/rdl/gen_regs.py

docs: regs
	@echo "HTML register docs: units/axil_shell/rdl/gen/html/index.html"

clean:
	rm -rf build sim_build .pytest_cache
	find . -name '.pytest_cache' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name 'rdl/gen' -type d -exec rm -rf {} + 2>/dev/null || true
	find units -path '*/dv/regmap.py' -delete 2>/dev/null || true
	find . -maxdepth 2 -name '*-rdl_regs.core' -delete 2>/dev/null || true
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.result.xml' -delete 2>/dev/null || true
	find . -name 'dump.vcd' -delete 2>/dev/null || true
	find . -name 'dump.fst' -delete 2>/dev/null || true
	$(MAKE) -C top/host clean 2>/dev/null || true

distclean: clean
	rm -rf .venv
