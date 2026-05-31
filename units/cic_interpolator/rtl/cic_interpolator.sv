// =============================================================================
// cic_interpolator.sv
//
// Cascaded Integrator-Comb (CIC) interpolation filter, AXI4-Stream in/out.
//
//   in samples ->  [N comb stages]  ->  upsample by R  ->  [N integrators]
//                  (input rate,                            (output rate)
//                   differential delay M)
//
// Each input sample produces R output samples. Upsampling is "zero-stuff":
// the comb output enters the integrator chain on the first of R output
// cycles, and zeros enter on the next R-1 output cycles. The integrators
// smear the impulse out into a continuous output stream.
//
// Bit growth: INTERNAL_W = IN_W + ceil(log2((R*M)^N)). Integrators wrap
// modulo 2^INTERNAL_W; comb cancellation upstream produces the right
// answer. Output is the top OUT_W bits of the post-update integrator
// tail.
//
// State machine
// -------------
// S_IDLE  — accept input. comb chain runs on the cycle of input handshake,
//           result captured into comb_captured. Transitions to S_RUN.
// S_RUN   — produce INTERP outputs over INTERP cycles. Cycle 0 feeds
//           comb_captured into the integrator chain; cycles 1..INTERP-1
//           feed zero. Burst counter pauses under output backpressure.
//
// Backpressure
// ------------
// s_axis_tready = (state == S_IDLE) — input blocked during a burst.
// Within a burst, if the consumer holds m_axis_tready low while we have
// an output sample, the burst counter does not advance; the integrator
// next-state is held.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module cic_interpolator #(
    parameter int unsigned STAGES = 3,
    parameter int unsigned INTERP = 4,
    parameter int unsigned DELAY  = 1,
    parameter int unsigned IN_W   = 16,
    parameter int unsigned OUT_W  = 16
) (
    input  wire                     clk,
    input  wire                     rst_n,

    input  wire signed [IN_W-1:0]   s_axis_tdata,
    input  wire                     s_axis_tvalid,
    output wire                     s_axis_tready,

    output wire signed [OUT_W-1:0]  m_axis_tdata,
    output wire                     m_axis_tvalid,
    input  wire                     m_axis_tready
);

    function automatic int gain_bits(int r, int m, int n);
        int g; g = 1;
        for (int i = 0; i < n; i++) g = g * r * m;
        return (g > 1) ? $clog2(g) : 0;
    endfunction

    localparam int GAIN_BITS  = gain_bits(INTERP, DELAY, STAGES);
    localparam int INTERNAL_W = IN_W + GAIN_BITS;
    localparam int CNT_W      = (INTERP > 1) ? $clog2(INTERP) : 1;

    // ---- State machine -------------------------------------------------
    typedef enum logic {S_IDLE, S_RUN} state_e;
    state_e state;
    reg [CNT_W-1:0] burst_cnt;

    // ---- Comb stages (combinational chain + registered history) -------
    reg signed [INTERNAL_W-1:0] comb_history [STAGES][DELAY];
    reg signed [INTERNAL_W-1:0] comb_captured;

    // ---- Integrators (pipelined register chain) ------------------------
    reg signed [INTERNAL_W-1:0] integ [STAGES];

    // ---- Output register ----------------------------------------------
    reg signed [OUT_W-1:0]      out_data;
    reg                         out_valid;

    wire input_handshake  = s_axis_tvalid && s_axis_tready;
    wire output_handshake = m_axis_tvalid && m_axis_tready;

    assign s_axis_tready = (state == S_IDLE);
    assign m_axis_tvalid = out_valid;
    assign m_axis_tdata  = out_data;

    // ---- Comb chain (combinational) ------------------------------------
    // verilator lint_off UNOPTFLAT
    wire signed [STAGES*INTERNAL_W-1:0] comb_chain_in_flat;
    wire signed [STAGES*INTERNAL_W-1:0] comb_chain_out_flat;

    for (genvar k = 0; k < STAGES; k++) begin : g_comb
        wire signed [INTERNAL_W-1:0] cin;
        wire signed [INTERNAL_W-1:0] cout;

        if (k == 0) begin : g_first
            assign cin = INTERNAL_W'(s_axis_tdata);
        end else begin : g_rest
            assign cin =
                comb_chain_out_flat[(k-1)*INTERNAL_W +: INTERNAL_W];
        end
        assign cout = cin - comb_history[k][DELAY-1];

        assign comb_chain_in_flat[k*INTERNAL_W +: INTERNAL_W]  = cin;
        assign comb_chain_out_flat[k*INTERNAL_W +: INTERNAL_W] = cout;
    end
    // verilator lint_on UNOPTFLAT

    wire signed [INTERNAL_W-1:0] comb_result =
        comb_chain_out_flat[(STAGES-1)*INTERNAL_W +: INTERNAL_W];

    // ---- Integrator next-state (combinational) -------------------------
    // integ_in_now is the chain input for the current burst cycle:
    // comb_captured on burst_cnt==0, zero otherwise. Used for both the
    // sequential integrator update and the combinational output sample.
    wire signed [INTERNAL_W-1:0] integ_in_now =
        ((state == S_RUN) && (burst_cnt == '0)) ? comb_captured
                                                 : {INTERNAL_W{1'b0}};

    // new_integ_tail = next-cycle value of integ[STAGES-1], computed
    // combinationally. Out_data captures the top OUT_W bits of this on
    // each advance. For STAGES==1 the tail is integ[0]+integ_in; for
    // STAGES>=2 it is integ[STAGES-1] + integ[STAGES-2] (matches the
    // Python reference: new_integ[N-1] = old_integ[N-1] + old_integ[N-2]).
    wire signed [INTERNAL_W-1:0] new_integ_tail;
    if (STAGES == 1) begin : g_single_stage
        assign new_integ_tail = integ[0] + integ_in_now;
    end else begin : g_multi_stage
        assign new_integ_tail = integ[STAGES-1] + integ[STAGES-2];
    end

    // ---- Sequential logic ----------------------------------------------
    integer i, j;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= S_IDLE;
            burst_cnt     <= '0;
            comb_captured <= '0;
            for (i = 0; i < STAGES; i++) begin
                for (j = 0; j < DELAY; j++) comb_history[i][j] <= '0;
                integ[i] <= '0;
            end
            out_data  <= '0;
            out_valid <= 1'b0;
        end else begin
            // Output handshake clears valid (advance below may set it 1
            // again in the same cycle).
            if (output_handshake) out_valid <= 1'b0;

            case (state)
                S_IDLE: begin
                    if (input_handshake) begin
                        for (i = 0; i < STAGES; i++) begin
                            comb_history[i][0] <=
                                comb_chain_in_flat[i*INTERNAL_W +: INTERNAL_W];
                            for (j = 1; j < DELAY; j++)
                                comb_history[i][j] <= comb_history[i][j-1];
                        end
                        comb_captured <= comb_result;
                        burst_cnt     <= '0;
                        state         <= S_RUN;
                    end
                end

                S_RUN: begin
                    if (!out_valid || output_handshake) begin
                        integ[0] <= integ[0] + integ_in_now;
                        for (i = 1; i < STAGES; i++) begin
                            integ[i] <= integ[i] + integ[i-1];
                        end
                        out_data <=
                            new_integ_tail[INTERNAL_W-1 -: OUT_W];
                        out_valid <= 1'b1;

                        if (burst_cnt == CNT_W'(INTERP - 1)) begin
                            burst_cnt <= '0;
                            state     <= S_IDLE;
                        end else begin
                            burst_cnt <= burst_cnt + 1'b1;
                        end
                    end
                end
            endcase
        end
    end

endmodule

`default_nettype wire
