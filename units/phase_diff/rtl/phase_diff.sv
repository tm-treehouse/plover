// =============================================================================
// phase_diff.sv
//
// Phase differentiator with implicit unwrap. The third piece of the FM
// receive chain (after AGC and CORDIC): turns the instantaneous phase
// stream from the CORDIC into the instantaneous frequency stream,
// which IS the demodulated audio for FM broadcast.
//
// Algorithm: out[n] = phase[n] - phase[n-1], with signed-modular
// arithmetic in PHASE_W bits. The bit-width subtraction handles the
// +-pi wrap automatically — when phase crosses +-pi, the raw
// difference is a near-2*pi jump (about +-2^PHASE_W), but the signed
// PHASE_W-bit subtractor wraps that to the small physical-frequency
// step. No explicit "if |diff| > pi" branch needed.
//
// Phase encoding (matches CORDIC and NCO):
//   signed PHASE_W bits, +pi mapped to +2^(PHASE_W-1) - 1,
//   -pi mapped to -2^(PHASE_W-1).
//
// Output represents normalized frequency in cycles per sample, signed
// PHASE_W bits. +/-0.5 cycles/sample (i.e. +/-Nyquist) maps to
// +/-2^(PHASE_W-1). For FM broadcast the modulation deviation per
// sample is small, so the output amplitude is small relative to full
// scale — that's normal.
//
// Pipeline depth: 2 cycles (register the incoming phase, then register
// the subtraction result). Output beat N corresponds to input beat N;
// the first emitted beat (after the first input arrives) has a
// meaningless value since there's no prior phase. The Python model
// emits the same first-beat value so bit-exactness holds; consumers
// should ignore the first beat after reset, same as any settling
// pipeline.
//
// No AXI-Lite — pure datapath, nothing software-tunable.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module phase_diff #(
    parameter int unsigned PHASE_W = 16
) (
    input  wire                    clk,
    input  wire                    rst_n,

    // ---- AXIS slave: instantaneous phase ----
    input  wire [PHASE_W-1:0]      s_axis_tdata,
    input  wire                    s_axis_tvalid,
    output wire                    s_axis_tready,

    // ---- AXIS master: instantaneous frequency ----
    output wire [PHASE_W-1:0]      m_axis_tdata,
    output wire                    m_axis_tvalid,
    input  wire                    m_axis_tready
);

    // ---- Handshake ----
    // The pipeline advances whenever the output can accept a beat
    // (consumer ready or output register empty).
    reg                            out_valid_q;
    wire pipeline_advance = !out_valid_q || m_axis_tready;
    assign s_axis_tready = pipeline_advance;

    // ---- Pipeline state ----
    // phase_q  : last accepted phase sample (the subtrahend).
    // freq_q   : registered output value.
    reg signed [PHASE_W-1:0]       phase_q;
    reg signed [PHASE_W-1:0]       freq_q;

    wire signed [PHASE_W-1:0]      in_phase  = s_axis_tdata;
    wire signed [PHASE_W-1:0]      diff_now  = in_phase - phase_q;

    assign m_axis_tvalid = out_valid_q;
    assign m_axis_tdata  = freq_q;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            phase_q     <= '0;
            freq_q      <= '0;
            out_valid_q <= 1'b0;
        end else if (pipeline_advance) begin
            if (s_axis_tvalid) begin
                // Compute diff against the *previous* phase, register
                // it as the output. Then update phase_q to the *new*
                // phase for the next cycle's subtraction.
                freq_q      <= diff_now;
                phase_q     <= in_phase;
                out_valid_q <= 1'b1;
            end else begin
                out_valid_q <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
