# =============================================================================
# plover.sdc — synthesis timing constraints (stub)
#
# Vendor-agnostic SDC. Most synthesis tools (Vivado, Quartus, yosys/nextpnr
# via abc) accept some subset of SDC; the parts here are the conservative
# common ground. Tighten and extend once a target board is picked.
#
# Replace the placeholder period below with the real target frequency.
# =============================================================================

# 100 MHz placeholder. Set this to whatever your board's oscillator gives you.
create_clock -name clk -period 10.000 [get_ports clk]

# Asynchronous reset assumed; relax timing on rst_n.
set_false_path -from [get_ports rst_n]

# I/O delays — set realistic values once a board and pinout are chosen.
# set_input_delay  -clock clk 2.000 [all_inputs]
# set_output_delay -clock clk 2.000 [all_outputs]
