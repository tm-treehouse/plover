// =============================================================================
// deemphasis.sv
//
// First-order IIR low-pass filter for FM broadcast de-emphasis. The
// fourth piece of the FM receive chain (after AGC, CORDIC, and
// phase_diff): inverts the transmitter's pre-emphasis high-shelf boost
// to restore flat audio response while leaving FM-triangle noise
// pre-attenuated.
//
// Standard time constants:
//   tau = 75 us  -- US, Japan
//   tau = 50 us  -- Europe, rest of world
//
// Implementation choice: single-pole IIR (exponential smoother), not
// the strict bilinear-transform biquad with one zero at z=-1. Reasons:
//
//   1. The DC blocker is also a single-pole IIR — same register
//      topology, the project pattern is established.
//   2. One coefficient (alpha) -> one AXI-Lite register, simpler
//      programming.
//   3. The spec deviation from a strict bilinear discretisation is
//      tiny in the audio passband; nobody hears the difference.
//      Broadcast receivers vary in exactly how they implement
//      de-emphasis — "75 us time constant" is the spec, not "exact
//      bilinear curve."
//
// Difference equation:
//
//   y[n] = (1 - alpha) * x[n] + alpha * y[n-1]
//
// where alpha = exp(-1 / (fs * tau)). For fs = 48 kHz and tau = 75 us,
// alpha ~ 0.7574. The RTL works with the integer Q-format
// representation; software picks alpha to match the actual sample
// rate and target time constant.
//
// Transfer function:
//
//   H(z) = (1 - alpha) / (1 - alpha * z^-1)
//
// Pole at z = alpha. DC gain = 1 (exact: (1-alpha) / (1-alpha) = 1).
// High-frequency rolloff is -6 dB/octave above the corner frequency
// fc ~ -ln(alpha) * fs / (2*pi).
//
// Bit growth and output formation
// -------------------------------
// * Output register y_prev holds the last output sample, SAMPLE_W bits
//   signed.
// * (MAX_COEF - alpha) represents (1 - alpha) in the same Q-format as
//   alpha; computed combinationally from alpha as (1<<COEF_FRAC_W) -
//   alpha. With Q1.15 coefficients, MAX_COEF is 1<<15 = 32768 (one
//   more than alpha's representable range), so (MAX_COEF - alpha) is
//   always non-negative.
// * (MAX_COEF - alpha) * x[n]  -> SAMPLE_W + COEF_W + 1 bits signed
// * alpha * y_prev              -> SAMPLE_W + COEF_W + 1 bits signed
// * sum                          -> SAMPLE_W + COEF_W + 2 bits signed
// * arithmetic right shift by COEF_FRAC_W
// * truncate to SAMPLE_W signed bits
//
// AXI-Lite coefficient register
// -----------------------------
// One 32-bit register at byte offset 0x00 holds alpha (signed COEF_W
// bits, Q-format). Reset value is computed to be roughly correct for
// a "typical" rate (48 kHz, 75 us): alpha ~ 0.7574 in Q1.15 = 24820 =
// 0x60F4. Software programs the exact value for its sample rate.
// Readback is sign-extended to 32 bits.
//
// AXIS data interface
// -------------------
// Standard slave-in / master-out, *real-valued* SAMPLE_W-bit samples
// (NOT IQ — the de-emphasis filter sits in the audio path, after FM
// demod). Output is registered (one cycle through-latency).
//
// Honest caveat on truncation
// ---------------------------
// Plain truncation in the feedback path floors toward negative
// infinity for negative values, which produces a small DC drift over
// time for inputs near zero. Same caveat as the DC blocker. For
// audio-rate processing of FM-demod output (which is centred near
// zero post-DC-blocker) this is a few-LSB effect, well below the
// audio noise floor.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module deemphasis #(
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    parameter int unsigned COEF_W         = 16,
    parameter int unsigned COEF_INT_W     = 1,
    parameter int unsigned COEF_FRAC_W    = COEF_W - COEF_INT_W
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXI-Lite slave ----
    input  wire [31:0]                   s_axil_awaddr,
    input  wire [2:0]                    s_axil_awprot,
    input  wire                          s_axil_awvalid,
    output wire                          s_axil_awready,

    input  wire [31:0]                   s_axil_wdata,
    input  wire [3:0]                    s_axil_wstrb,
    input  wire                          s_axil_wvalid,
    output wire                          s_axil_wready,

    output wire [1:0]                    s_axil_bresp,
    output wire                          s_axil_bvalid,
    input  wire                          s_axil_bready,

    input  wire [31:0]                   s_axil_araddr,
    input  wire [2:0]                    s_axil_arprot,
    input  wire                          s_axil_arvalid,
    output wire                          s_axil_arready,

    output wire [31:0]                   s_axil_rdata,
    output wire [1:0]                    s_axil_rresp,
    output wire                          s_axil_rvalid,
    input  wire                          s_axil_rready,

    // ---- AXIS slave: real-valued samples ----
    input  wire [SAMPLE_W-1:0]           s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: real-valued samples ----
    output wire [SAMPLE_W-1:0]           m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    // ---- Q-format consistency checks ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "deemphasis: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
        if (COEF_INT_W + COEF_FRAC_W != COEF_W)
            $fatal(1, "deemphasis: COEF_INT_W (%0d) + COEF_FRAC_W (%0d) != COEF_W (%0d)",
                   COEF_INT_W, COEF_FRAC_W, COEF_W);
    end

    localparam logic [1:0]  RESP_OKAY   = 2'b00;
    localparam logic [1:0]  RESP_DECERR = 2'b11;
    localparam logic [31:0] ADDR_MASK   = 32'h0000_0003;

    // Reset default for alpha. Computed at elaboration as the Q1.15
    // value of exp(-1/(48000 * 75e-6)) ~ 0.7574 ~ 24820 ~ 0x60F4.
    // Software should program the exact value for the actual rate.
    localparam logic signed [COEF_W-1:0] ALPHA_DEFAULT = COEF_W'(24820);

    // ====================================================================
    // AXI-Lite slave — one coefficient register
    // ====================================================================

    reg signed [COEF_W-1:0] alpha;
    reg [31:0] aw_addr_q;
    reg        aw_seen_q;
    reg [31:0] w_data_q;
    reg        w_seen_q;
    reg        b_valid_q;
    reg [1:0]  b_resp_q;
    reg [31:0] ar_addr_q;
    reg        ar_seen_q;
    reg        r_valid_q;
    reg [31:0] r_data_q;
    reg [1:0]  r_resp_q;

    assign s_axil_awready = !aw_seen_q;
    assign s_axil_wready  = !w_seen_q;
    assign s_axil_bvalid  = b_valid_q;
    assign s_axil_bresp   = b_resp_q;
    assign s_axil_arready = !ar_seen_q;
    assign s_axil_rvalid  = r_valid_q;
    assign s_axil_rdata   = r_data_q;
    assign s_axil_rresp   = r_resp_q;

    wire [31:0] aw_masked = aw_addr_q & ADDR_MASK;
    wire [31:0] ar_masked = ar_addr_q & ADDR_MASK;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            alpha       <= ALPHA_DEFAULT;
            aw_addr_q   <= '0; aw_seen_q <= 1'b0;
            w_data_q    <= '0; w_seen_q  <= 1'b0;
            b_valid_q   <= 1'b0; b_resp_q <= RESP_OKAY;
            ar_addr_q   <= '0; ar_seen_q <= 1'b0;
            r_valid_q   <= 1'b0; r_data_q <= '0; r_resp_q <= RESP_OKAY;
        end else begin
            if (s_axil_awvalid && s_axil_awready) begin
                aw_addr_q <= s_axil_awaddr; aw_seen_q <= 1'b1;
            end
            if (s_axil_wvalid && s_axil_wready) begin
                w_data_q <= s_axil_wdata; w_seen_q <= 1'b1;
            end
            if (aw_seen_q && w_seen_q && !b_valid_q) begin
                b_valid_q <= 1'b1;
                b_resp_q  <= RESP_OKAY;
                aw_seen_q <= 1'b0;
                w_seen_q  <= 1'b0;
                case (aw_masked[1:0])
                    2'b00: alpha <= w_data_q[COEF_W-1:0];
                    default: b_resp_q <= RESP_DECERR;
                endcase
            end
            if (s_axil_bvalid && s_axil_bready) b_valid_q <= 1'b0;

            if (s_axil_arvalid && s_axil_arready) begin
                ar_addr_q <= s_axil_araddr; ar_seen_q <= 1'b1;
            end
            if (ar_seen_q && !r_valid_q) begin
                r_valid_q <= 1'b1;
                r_resp_q  <= RESP_OKAY;
                case (ar_masked[1:0])
                    2'b00: r_data_q <= {{(32-COEF_W){alpha[COEF_W-1]}}, alpha};
                    default: begin
                        r_data_q <= 32'h0;
                        r_resp_q <= RESP_DECERR;
                    end
                endcase
                ar_seen_q <= 1'b0;
            end
            if (s_axil_rvalid && s_axil_rready) r_valid_q <= 1'b0;
        end
    end

    // ====================================================================
    // IIR datapath
    // ====================================================================

    // (1 - alpha) in the same Q-format as alpha. MAX_COEF = 1<<COEF_FRAC_W
    // is the integer representation of 1.0. Result is non-negative for
    // alpha in [0, 1.0).
    localparam logic signed [COEF_W:0] MAX_COEF = (COEF_W+1)'(1) <<< COEF_FRAC_W;
    wire signed [COEF_W:0]   one_minus_alpha = MAX_COEF - {alpha[COEF_W-1], alpha};

    wire signed [SAMPLE_W-1:0] in_sample = s_axis_tdata;

    // y_prev: registered last output sample.
    reg signed [SAMPLE_W-1:0]  y_prev;
    reg signed [SAMPLE_W-1:0]  out_q;
    reg                        out_valid_q;

    wire pipeline_advance = !out_valid_q || m_axis_tready;
    wire input_fire = s_axis_tvalid && pipeline_advance;

    assign s_axis_tready = pipeline_advance;
    assign m_axis_tvalid = out_valid_q;
    assign m_axis_tdata  = out_q;

    // (1 - alpha) * x[n] : (COEF_W + 1) + SAMPLE_W bits signed
    wire signed [COEF_W + SAMPLE_W : 0] prod_in = one_minus_alpha * in_sample;
    // alpha * y[n-1]     : COEF_W + SAMPLE_W bits signed
    wire signed [COEF_W + SAMPLE_W - 1 : 0] prod_fb = alpha * y_prev;
    // Sum with one extra bit of headroom (sign-extend the narrower product).
    wire signed [COEF_W + SAMPLE_W : 0] prod_fb_ext = {prod_fb[COEF_W + SAMPLE_W - 1], prod_fb};
    wire signed [COEF_W + SAMPLE_W + 1 : 0] sum_full =
        {prod_in[COEF_W + SAMPLE_W], prod_in} + {prod_fb_ext[COEF_W + SAMPLE_W], prod_fb_ext};
    // Arithmetic right shift to bring back to SAMPLE_W's Q-position.
    wire signed [COEF_W + SAMPLE_W + 1 : 0] sum_shifted = sum_full >>> COEF_FRAC_W;
    // Truncate to SAMPLE_W bits (low bits of the shifted value, matching
    // FIR convention).
    wire signed [SAMPLE_W - 1 : 0] y_next = sum_shifted[SAMPLE_W - 1 : 0];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            y_prev      <= '0;
            out_q       <= '0;
            out_valid_q <= 1'b0;
        end else if (pipeline_advance) begin
            if (input_fire) begin
                y_prev      <= y_next;
                out_q       <= y_next;
                out_valid_q <= 1'b1;
            end else begin
                out_valid_q <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
