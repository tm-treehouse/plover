// =============================================================================
// cic_decimator.sv
//
// Cascaded Integrator-Comb (CIC) decimation filter, AXI4-Stream in/out.
//
//   in samples ->  [N integrators]  ->  decimate by R  ->  [N comb stages]
//                  (run at input rate)                    (run at output rate,
//                                                          differential delay M)
//
// Bit growth: gain = (R*M)^N, so internal width = IN_W + ceil(log2((R*M)^N)).
// Integrators wrap modulo 2^INTERNAL_W — this is *expected* and is exactly
// recovered by the combs subtracting back out. Output is the top OUT_W bits
// of the final comb output (truncating LSBs).
//
// Backpressure
// ------------
// Standard AXIS: s_axis_tready goes low when an output is held pending
// consumption (m_axis_tvalid=1 && m_axis_tready=0). Under sustained
// backpressure the unit stalls input, which propagates upstream.
//
// Pipeline shape
// --------------
// * Integrators: one register per stage, advancing on input handshake.
//   Sequential add chain inside a single clock cycle (combinational depth
//   N adders). For N=3..5 and typical FPGA fmax this is fine; if it bites,
//   split the chain across multiple cycles or insert skid buffers.
// * Decimator: counter mod R, increments on input handshake. When it
//   wraps, sets ``decim_event`` for the next cycle so the (updated)
//   integ[N-1] value flows into the combs.
// * Combs: combinational chain of N subtractors fed from registered
//   comb_history. On decim_event, the chain output is registered into
//   out_data and history shifts. Combinational depth N subtractors.
//
// Parameters
// ----------
// STAGES   — N, integrator/comb count (typically 3-5).
// DECIM    — R, decimation factor.
// DELAY    — M, differential delay (typically 1, sometimes 2).
// IN_W     — signed input width.
// OUT_W    — signed output width (top OUT_W bits of internal result).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module cic_decimator #(
    parameter int unsigned STAGES = 3,
    parameter int unsigned DECIM  = 4,
    parameter int unsigned DELAY  = 1,
    parameter int unsigned IN_W   = 16,
    parameter int unsigned OUT_W  = 16
) (
    input  wire                     clk,
    input  wire                     rst_n,

    // ---- AXIS slave (input samples) ----
    input  wire signed [IN_W-1:0]   s_axis_tdata,
    input  wire                     s_axis_tvalid,
    output wire                     s_axis_tready,

    // ---- AXIS master (decimated output samples) ----
    output wire signed [OUT_W-1:0]  m_axis_tdata,
    output wire                     m_axis_tvalid,
    input  wire                     m_axis_tready
);

    // Bit growth: ceil(log2((R*M)^N)). $clog2(x) in SV returns ceil(log2(x))
    // for x>=1 (with $clog2(1)=0). For non-power-of-2 R*M, this may slightly
    // overprovision compared to ceil(log2((R*M)^N)) computed exactly, but
    // the safe upper bound is fine and matches the Python model.
    function automatic int gain_bits(int r, int m, int n);
        int g; g = 1;
        for (int i = 0; i < n; i++) g = g * r * m;
        return (g > 1) ? $clog2(g) : 0;
    endfunction

    localparam int GAIN_BITS  = gain_bits(DECIM, DELAY, STAGES);
    localparam int INTERNAL_W = IN_W + GAIN_BITS;
    localparam int CNT_W      = (DECIM > 1) ? $clog2(DECIM) : 1;

    // ---- State ---------------------------------------------------------
    // Integrator stages: one register each.
    reg signed [INTERNAL_W-1:0] integ [STAGES];

    // Comb history: per-stage, M-deep delay line. comb_history[i][0] is the
    // most recently observed input to comb stage i; [M-1] is the oldest
    // (the value subtracted out).
    reg signed [INTERNAL_W-1:0] comb_history [STAGES][DELAY];

    reg [CNT_W-1:0]              decim_cnt;
    reg                          decim_event_d;  // 1-cycle pulse, delayed
    reg signed [OUT_W-1:0]       out_data;
    reg                          out_valid;

    wire input_handshake  = s_axis_tvalid && s_axis_tready;
    wire output_handshake = m_axis_tvalid && m_axis_tready;
    wire at_decim_point   = (decim_cnt == CNT_W'(DECIM - 1));

    // ---- Backpressure --------------------------------------------------
    // Block input only when output is pending and not being consumed; in
    // that state, accepting an Rth input would mean producing a new output
    // we have nowhere to put.
    assign s_axis_tready = !(out_valid && !m_axis_tready);
    assign m_axis_tvalid = out_valid;
    assign m_axis_tdata  = out_data;

    // ---- Comb chain (combinational) ------------------------------------
    // Each stage subtracts the M-th previous input from the current.
    // Combinational chain of N subtractors; result is registered on
    // decim_event_d.
    //
    // Note: the combinational-loop detector in verilator raises a false
    // positive on the chain comb_chain_out[k-1] -> comb_chain_in[k] ->
    // comb_chain_out[k] across generate iterations, even though each
    // element is uniquely driven. Each stages combinational path is
    // bounded by a registered comb_history[k][DELAY-1] term, so there
    // is no actual loop. We locally disable UNOPTFLAT for this block.
    // verilator lint_off UNOPTFLAT
    wire signed [STAGES*INTERNAL_W-1:0] comb_chain_in_flat;
    wire signed [STAGES*INTERNAL_W-1:0] comb_chain_out_flat;

    for (genvar k = 0; k < STAGES; k++) begin : g_comb
        wire signed [INTERNAL_W-1:0] cin;
        wire signed [INTERNAL_W-1:0] cout;

        if (k == 0) begin : g_first
            assign cin = integ[STAGES-1];
        end else begin : g_rest
            assign cin =
                comb_chain_out_flat[(k-1)*INTERNAL_W +: INTERNAL_W];
        end
        assign cout = cin - comb_history[k][DELAY-1];

        assign comb_chain_in_flat[k*INTERNAL_W +: INTERNAL_W]  = cin;
        assign comb_chain_out_flat[k*INTERNAL_W +: INTERNAL_W] = cout;
    end
    // verilator lint_on UNOPTFLAT

    // ---- Sequential logic ----------------------------------------------
    integer i, j;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < STAGES; i++) begin
                integ[i] <= '0;
                for (j = 0; j < DELAY; j++) comb_history[i][j] <= '0;
            end
            decim_cnt     <= '0;
            decim_event_d <= 1'b0;
            out_data      <= '0;
            out_valid     <= 1'b0;
        end else begin
            // Default: decim_event_d clears each cycle unless re-asserted
            decim_event_d <= 1'b0;

            // Output handshake clears the pending-output flag
            if (output_handshake) out_valid <= 1'b0;

            // Integrators advance on input handshake
            if (input_handshake) begin
                integ[0] <= integ[0] + INTERNAL_W'(s_axis_tdata);
                for (i = 1; i < STAGES; i++) begin
                    integ[i] <= integ[i] + integ[i-1];
                end
                // Decimation counter & event scheduling
                if (at_decim_point) begin
                    decim_cnt     <= '0;
                    decim_event_d <= 1'b1;
                end else begin
                    decim_cnt <= decim_cnt + 1'b1;
                end
            end

            // Combs fire one cycle after decimation event (so they see
            // the updated integ[STAGES-1]).
            if (decim_event_d) begin
                // Shift history: history[k][0] gets the current chain input
                // for stage k; older entries shift right.
                for (i = 0; i < STAGES; i++) begin
                    comb_history[i][0] <=
                        comb_chain_in_flat[i*INTERNAL_W +: INTERNAL_W];
                    for (j = 1; j < DELAY; j++)
                        comb_history[i][j] <= comb_history[i][j-1];
                end
                // Output: top OUT_W bits of final comb stage output
                out_data <= comb_chain_out_flat[
                    (STAGES-1)*INTERNAL_W + (INTERNAL_W - OUT_W) +: OUT_W];
                out_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
