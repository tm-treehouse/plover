// =============================================================================
// complex_mixer.sv
//
// Complex multiplier ("mixer" in the SDR sense). Multiplies two complex
// streams beat-by-beat:
//
//     out = a * b
//         = (I_a + j Q_a) * (I_b + j Q_b)
//         = (I_a*I_b - Q_a*Q_b) + j * (I_a*Q_b + Q_a*I_b)
//
// Both inputs and the output carry packed complex samples on AXIS, with
// the same packing convention as the NCO: TDATA is 2*SAMPLE_W bits with
// Q in the upper half and I in the lower half.
//
// Architecture
// ------------
// Pure datapath with one register stage between the multiplier outputs
// and the AXIS master port. Four parallel multipliers + one add and one
// subtract:
//
//          a.I --+---*---+
//                |       |
//          b.I --+       +--[-]--> out_I
//                        |
//          a.Q --+---*---+
//                |
//          b.Q --+
//          ...etc symmetrically for Q.
//
// The multiplier outputs are 2*SAMPLE_W bits signed; the add/subtract
// adds a sign-extension bit (2*SAMPLE_W + 1 bits). The output formation
// arithmetic-right-shifts by OUT_SHIFT (default SAMPLE_FRAC_W) and
// takes the low SAMPLE_W bits — same convention as the FIR. With
// Q1.(SAMPLE_W-1) samples this preserves the input Q-position.
//
// Handshake
// ---------
// One output beat per pair of synchronised input beats. tready_a and
// tready_b both assert when the consumer is ready *and* both upstream
// producers have valid; tvalid_out asserts on the next cycle after
// both upstream handshakes fire.
//
// Why register output: each multiplier has its own latency in synthesis;
// the registered boundary keeps the timing closure local to this unit.
// Easier to constrain than a fully-combinational complex multiplier.
//
// No AXI-Lite: this is pure datapath, nothing software-tunable beyond
// width parameters.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module complex_mixer #(
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    // Right-shift applied to each sum/diff before truncation to
    // SAMPLE_W. Defaults to SAMPLE_FRAC_W so input Q-position is
    // preserved through the multiply.
    parameter int          OUT_SHIFT      = SAMPLE_FRAC_W
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXIS slave: stream A (complex) ----
    input  wire [2*SAMPLE_W-1:0]         s_axis_a_tdata,
    input  wire                          s_axis_a_tvalid,
    output wire                          s_axis_a_tready,

    // ---- AXIS slave: stream B (complex) ----
    input  wire [2*SAMPLE_W-1:0]         s_axis_b_tdata,
    input  wire                          s_axis_b_tvalid,
    output wire                          s_axis_b_tready,

    // ---- AXIS master: complex product ----
    output wire [2*SAMPLE_W-1:0]         m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    // ---- Q-format consistency check ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "complex_mixer: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
    end

    // ---- Unpack inputs ----
    wire signed [SAMPLE_W-1:0] a_i = s_axis_a_tdata[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0] a_q = s_axis_a_tdata[2*SAMPLE_W-1:SAMPLE_W];
    wire signed [SAMPLE_W-1:0] b_i = s_axis_b_tdata[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0] b_q = s_axis_b_tdata[2*SAMPLE_W-1:SAMPLE_W];

    // ---- Handshake. Both inputs must be valid; the output must be
    // ready (or empty). One output beat per synchronised pair. ----
    wire both_valid = s_axis_a_tvalid && s_axis_b_tvalid;
    reg  out_valid_q;
    wire output_ready_to_advance = !out_valid_q || m_axis_tready;
    wire input_fire = both_valid && output_ready_to_advance;

    assign s_axis_a_tready = input_fire;
    assign s_axis_b_tready = input_fire;
    assign m_axis_tvalid   = out_valid_q;

    // ---- Multipliers ----
    // 2*SAMPLE_W bits signed; with sign-bit add/sub these grow to
    // 2*SAMPLE_W+1 bits before shift+truncate.
    wire signed [2*SAMPLE_W-1:0] p_ii = a_i * b_i;
    wire signed [2*SAMPLE_W-1:0] p_qq = a_q * b_q;
    wire signed [2*SAMPLE_W-1:0] p_iq = a_i * b_q;
    wire signed [2*SAMPLE_W-1:0] p_qi = a_q * b_i;

    // Sum/difference with one bit of headroom.
    wire signed [2*SAMPLE_W:0]   sum_i_pre = p_ii - p_qq;   // out_I
    wire signed [2*SAMPLE_W:0]   sum_q_pre = p_iq + p_qi;   // out_Q

    // ---- Output formation: arithmetic right shift then take low
    // SAMPLE_W bits (same convention as fir_filter). ----
    wire signed [2*SAMPLE_W:0]   shifted_i = sum_i_pre >>> OUT_SHIFT;
    wire signed [2*SAMPLE_W:0]   shifted_q = sum_q_pre >>> OUT_SHIFT;
    wire signed [SAMPLE_W-1:0]   out_i_now = shifted_i[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0]   out_q_now = shifted_q[SAMPLE_W-1:0];

    // ---- Output register ----
    reg signed [SAMPLE_W-1:0] out_i_q, out_q_q;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            out_i_q     <= '0;
            out_q_q     <= '0;
            out_valid_q <= 1'b0;
        end else begin
            if (input_fire) begin
                out_i_q     <= out_i_now;
                out_q_q     <= out_q_now;
                out_valid_q <= 1'b1;
            end else if (m_axis_tready && out_valid_q) begin
                // Consumer drained the output and no new input pair
                // is available this cycle. Lower tvalid.
                out_valid_q <= 1'b0;
            end
        end
    end

    assign m_axis_tdata = { out_q_q, out_i_q };

endmodule

`default_nettype wire
