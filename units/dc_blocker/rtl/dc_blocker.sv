// =============================================================================
// dc_blocker.sv
//
// First-order IIR highpass filter — the simplest useful IIR. Removes the
// DC component of a signed sample stream by placing a zero at DC and a
// pole near DC.
//
// Transfer function:
//
//   H(z) = (1 - z^-1) / (1 - alpha * z^-1)
//
// Difference equation:
//
//   y[n] = x[n] - x[n-1] + alpha * y[n-1]
//
// alpha is a software-programmable feedback coefficient close to (but
// less than) 1.0 in Q1.(COEF_W-1). Larger alpha => tighter notch around
// DC and longer transient response; smaller alpha => wider notch and
// faster response. Typical SDR/audio values are 0.99 .. 0.9999.
//
// Bit growth and output formation
// -------------------------------
// * Sample register x_prev holds the last input sample, IN_W bits.
// * Output register y_prev holds the last output sample, OUT_W bits.
// * alpha * y_prev is COEF_W + OUT_W bits wide; after a signed right
//   shift by COEF_FRAC_W, it's back at OUT_W's Q-position.
// * y_next = (x - x_prev) + (alpha*y_prev >> COEF_FRAC_W). The
//   subtraction (x - x_prev) is IN_W+1 bits to handle full-scale
//   negation; everything is then truncated/extended into the OUT_W-
//   wide y_prev register. Plain truncation; no saturation, no
//   rounding — same convention as the FIR.
//
// AXI-Lite coefficient register
// -----------------------------
// One 32-bit AXI-Lite-addressable word at byte offset 0x00 holds
// alpha. The RTL takes the low COEF_W bits on write; readback is
// sign-extended to 32 bits. After reset alpha is zero — the filter
// acts as a pure differentiator y[n] = x[n] - x[n-1] until software
// programs a useful value. The integration tests must program alpha
// before expecting useful DC suppression.
//
// AXIS data interface
// -------------------
// Standard slave-in / master-out. One sample in, one sample out per
// handshake. Output is registered (one cycle latency through the
// filter). Backpressure: s_axis_tready drops when output is held.
//
// Honest caveat on rounding
// -------------------------
// Plain truncation in a feedback path drifts toward negative values
// (truncation rounds toward -infinity). The effective DC bias is small
// but non-zero. Documented here, not fixed — matches the project
// convention; can be revisited if a chain shows measurable bias.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module dc_blocker #(
    // Sample/coefficient widths and Q-format.
    parameter int unsigned IN_W        = 16,
    parameter int unsigned IN_INT_W    = 1,
    parameter int unsigned IN_FRAC_W   = IN_W - IN_INT_W,
    parameter int unsigned COEF_W      = 16,
    parameter int unsigned COEF_INT_W  = 1,
    parameter int unsigned COEF_FRAC_W = COEF_W - COEF_INT_W,
    parameter int unsigned OUT_W       = 16,
    parameter int unsigned OUT_INT_W   = 1,
    parameter int unsigned OUT_FRAC_W  = OUT_W - OUT_INT_W
) (
    input  wire                    clk,
    input  wire                    rst_n,

    // ---- AXI-Lite slave for alpha programming ----
    input  wire [31:0]             s_axil_awaddr,
    input  wire [2:0]              s_axil_awprot,
    input  wire                    s_axil_awvalid,
    output wire                    s_axil_awready,
    input  wire [31:0]             s_axil_wdata,
    input  wire [3:0]              s_axil_wstrb,
    input  wire                    s_axil_wvalid,
    output wire                    s_axil_wready,
    output wire [1:0]              s_axil_bresp,
    output wire                    s_axil_bvalid,
    input  wire                    s_axil_bready,
    input  wire [31:0]             s_axil_araddr,
    input  wire [2:0]              s_axil_arprot,
    input  wire                    s_axil_arvalid,
    output wire                    s_axil_arready,
    output wire [31:0]             s_axil_rdata,
    output wire [1:0]              s_axil_rresp,
    output wire                    s_axil_rvalid,
    input  wire                    s_axil_rready,

    // ---- AXIS slave (input samples) ----
    input  wire signed [IN_W-1:0]  s_axis_tdata,
    input  wire                    s_axis_tvalid,
    output wire                    s_axis_tready,

    // ---- AXIS master (DC-suppressed output samples) ----
    output wire signed [OUT_W-1:0] m_axis_tdata,
    output wire                    m_axis_tvalid,
    input  wire                    m_axis_tready
);

    localparam logic [1:0] RESP_OKAY   = 2'b00;
    localparam logic [1:0] RESP_DECERR = 2'b11;

    // Address bits used by the slave: only the low 2 bits matter
    // (single word at offset 0). Mask covers exactly one word.
    localparam logic [31:0] ADDR_MASK = 32'h0000_0003;

    // Product width: alpha * y_prev. Sum width: one extra bit above
    // OUT_W to absorb the (x - x_prev) full-scale negation case.
    localparam int PROD_W = COEF_W + OUT_W;

    // ---- Elaboration-time Q-format checks ----
    initial begin
        if (IN_INT_W + IN_FRAC_W != IN_W)
            $fatal(1, "dc_blocker: IN_INT_W (%0d) + IN_FRAC_W (%0d) != IN_W (%0d)",
                   IN_INT_W, IN_FRAC_W, IN_W);
        if (COEF_INT_W + COEF_FRAC_W != COEF_W)
            $fatal(1, "dc_blocker: COEF_INT_W (%0d) + COEF_FRAC_W (%0d) != COEF_W (%0d)",
                   COEF_INT_W, COEF_FRAC_W, COEF_W);
        if (OUT_INT_W + OUT_FRAC_W != OUT_W)
            $fatal(1, "dc_blocker: OUT_INT_W (%0d) + OUT_FRAC_W (%0d) != OUT_W (%0d)",
                   OUT_INT_W, OUT_FRAC_W, OUT_W);
    end

    // ====================================================================
    // AXI-Lite slave (single alpha register)
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

    // Word offset 0 is alpha; everything else is DECERR.
    wire [31:0] aw_masked    = aw_addr_q & ADDR_MASK;
    wire        aw_in_range  = (aw_masked == 32'h0);

    wire [31:0] ar_masked    = ar_addr_q & ADDR_MASK;
    wire        ar_in_range  = (ar_masked == 32'h0);

    wire [31:0] alpha_read_value =
        {{(32 - COEF_W){alpha[COEF_W-1]}}, alpha};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            alpha       <= '0;
            aw_addr_q   <= '0;
            aw_seen_q   <= 1'b0;
            w_data_q    <= '0;
            w_seen_q    <= 1'b0;
            b_valid_q   <= 1'b0;
            b_resp_q    <= RESP_OKAY;
            ar_addr_q   <= '0;
            ar_seen_q   <= 1'b0;
            r_valid_q   <= 1'b0;
            r_data_q    <= '0;
            r_resp_q    <= RESP_OKAY;
        end else begin
            if (s_axil_awvalid && s_axil_awready) begin
                aw_addr_q <= s_axil_awaddr;
                aw_seen_q <= 1'b1;
            end
            if (s_axil_wvalid && s_axil_wready) begin
                w_data_q <= s_axil_wdata;
                w_seen_q <= 1'b1;
            end
            if (aw_seen_q && w_seen_q && !b_valid_q) begin
                if (aw_in_range) begin
                    alpha    <= w_data_q[COEF_W-1:0];
                    b_resp_q <= RESP_OKAY;
                end else begin
                    b_resp_q <= RESP_DECERR;
                end
                b_valid_q <= 1'b1;
                aw_seen_q <= 1'b0;
                w_seen_q  <= 1'b0;
            end
            if (s_axil_bvalid && s_axil_bready) b_valid_q <= 1'b0;

            if (s_axil_arvalid && s_axil_arready) begin
                ar_addr_q <= s_axil_araddr;
                ar_seen_q <= 1'b1;
            end
            if (ar_seen_q && !r_valid_q) begin
                r_data_q  <= ar_in_range ? alpha_read_value : 32'h0;
                r_resp_q  <= ar_in_range ? RESP_OKAY : RESP_DECERR;
                r_valid_q <= 1'b1;
                ar_seen_q <= 1'b0;
            end
            if (s_axil_rvalid && s_axil_rready) r_valid_q <= 1'b0;
        end
    end

    // ====================================================================
    // DC-blocker datapath
    // ====================================================================

    reg signed [IN_W-1:0]  x_prev;
    reg signed [OUT_W-1:0] y_prev;
    reg signed [OUT_W-1:0] out_data;
    reg                    out_valid;

    wire input_handshake  = s_axis_tvalid && s_axis_tready;
    wire output_handshake = m_axis_tvalid && m_axis_tready;

    assign s_axis_tready = !(out_valid && !m_axis_tready);
    assign m_axis_tdata  = out_data;
    assign m_axis_tvalid = out_valid;

    // Compute next-state output combinationally from the *current*
    // input sample and the *prior* x_prev / y_prev. Registers update
    // at the end of the input handshake cycle.
    //
    // diff_term = x - x_prev. One bit wider than IN_W to handle the
    // case where x = INT16_MAX and x_prev = INT16_MIN (or vice versa).
    wire signed [IN_W:0]   diff_term = $signed({s_axis_tdata[IN_W-1], s_axis_tdata})
                                     - $signed({x_prev[IN_W-1], x_prev});

    // feedback_prod = alpha * y_prev. Width PROD_W = COEF_W + OUT_W.
    wire signed [PROD_W-1:0] feedback_prod = alpha * y_prev;
    // Shift right by COEF_FRAC_W to bring it back to OUT_W's Q-position.
    wire signed [PROD_W-1:0] feedback_shifted = feedback_prod >>> COEF_FRAC_W;
    // Take the low OUT_W bits of the shifted value (same convention as
    // the FIR — bits are truncated, not saturated).
    wire signed [OUT_W-1:0]  feedback_term = feedback_shifted[OUT_W-1:0];

    // y_next = (x - x_prev) + alpha*y_prev shifted. The first term is
    // (IN_W+1)-wide; the second is OUT_W-wide. Cast both to OUT_W via
    // sign-extension or truncation, then add.
    wire signed [OUT_W-1:0] diff_in_out_w = diff_term[OUT_W-1:0];
    wire signed [OUT_W-1:0] y_next        = diff_in_out_w + feedback_term;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x_prev    <= '0;
            y_prev    <= '0;
            out_data  <= '0;
            out_valid <= 1'b0;
        end else begin
            if (output_handshake) out_valid <= 1'b0;
            if (input_handshake) begin
                x_prev    <= s_axis_tdata;
                y_prev    <= y_next;
                out_data  <= y_next;
                out_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
