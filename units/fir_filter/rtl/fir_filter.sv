// =============================================================================
// fir_filter.sv
//
// Direct-form FIR filter, fully unfolded (one sample in per cycle, one
// sample out per cycle after pipeline fill). AXI-Stream sample interface
// plus AXI-Lite coefficient bank for hot-updatable taps.
//
// Architecture
// ------------
// * N_TAPS-deep signed sample shift register (sample_sr).
// * N_TAPS-entry coefficient memory (coef), each entry COEF_W bits signed.
// * Per-cycle MAC: combinational sum of element-wise products of the
//   (new-sample-shifted-in) sample SR and the current coef bank.
//
// Bit widths
// ----------
// * IN_W       — sample width
// * COEF_W     — coefficient width
// * Product    — IN_W + COEF_W bits signed
// * ACCUM_W    — IN_W + COEF_W + ceil(log2(N_TAPS)) bits (sum tree)
// * OUT_SHIFT  — right-shift before output truncation, defaults to
//                COEF_W-1 (preserves the input's Q-position when
//                coefficients are Q1.(COEF_W-1))
// * OUT_W      — output width; top OUT_W bits of the shifted accumulator.
//                Plain truncation; no saturation or rounding.
//
// AXI-Lite coefficient bank
// -------------------------
// 32-bit AXI-Lite slave. Byte offset 4*i targets coef[i]. Word size is
// fixed at 32; coefficients narrower than 32 bits are sign-extended on
// read. Writes outside [0, N_TAPS) return DECERR; reads outside return
// DECERR with zero data.
//
// Hot-update: writes are committed on B-channel handshake and visible
// to the next input sample's MAC. No double-buffering. Software is
// expected to know what it is doing when changing coefs mid-stream.
//
// Backpressure
// ------------
// * s_axis_tready = !(out_valid && !m_axis_tready) — block input when
//   the output beat is held.
// * AXI-Lite slave is independent of the AXIS data path; it always
//   accepts writes/reads (single-cycle response).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module fir_filter #(
    parameter int unsigned N_TAPS    = 8,
    parameter int unsigned IN_W      = 16,
    parameter int unsigned COEF_W    = 16,
    parameter int unsigned OUT_W     = 16,
    parameter int          OUT_SHIFT = COEF_W - 1
) (
    input  wire                    clk,
    input  wire                    rst_n,

    // ---- AXI-Lite slave for coefficient writes ----
    // 32-bit data, 32-bit address (only low ADDR_W bits used).
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

    // ---- AXIS master (filtered output samples) ----
    output wire signed [OUT_W-1:0] m_axis_tdata,
    output wire                    m_axis_tvalid,
    input  wire                    m_axis_tready
);

    // Width of the per-element product.
    localparam int PROD_W  = IN_W + COEF_W;
    // ceil(log2(N_TAPS)) extra bits for the sum tree; clamp at 1 to
    // avoid $clog2(1)=0 underflow when N_TAPS==1.
    localparam int TAP_BITS = (N_TAPS > 1) ? $clog2(N_TAPS) : 1;
    localparam int ACCUM_W  = PROD_W + TAP_BITS;
    // Address bits needed to enumerate N_TAPS words; min 1.
    localparam int IDX_BITS = (N_TAPS > 1) ? $clog2(N_TAPS) : 1;

    localparam logic [1:0] RESP_OKAY   = 2'b00;
    localparam logic [1:0] RESP_DECERR = 2'b11;

    // ====================================================================
    // Sample shift register and MAC
    // ====================================================================

    reg signed [IN_W-1:0]   sample_sr [N_TAPS];
    reg signed [COEF_W-1:0] coef      [N_TAPS];

    // sample_sr_next: combinational, with the new sample at index 0
    // when an input handshake is happening this cycle. Used both for
    // the MAC and for the next-cycle shift register state.
    wire signed [IN_W-1:0] sample_sr_next [N_TAPS];
    wire input_handshake  = s_axis_tvalid && s_axis_tready;
    wire output_handshake = m_axis_tvalid && m_axis_tready;

    for (genvar i = 0; i < N_TAPS; i++) begin : g_sr_next
        if (i == 0) begin : g_sr_first
            assign sample_sr_next[i] =
                input_handshake ? s_axis_tdata : sample_sr[0];
        end else begin : g_sr_rest
            assign sample_sr_next[i] =
                input_handshake ? sample_sr[i-1] : sample_sr[i];
        end
    end

    // Combinational MAC: products + sum tree. For modest N_TAPS this
    // is fine; for large N_TAPS one would pipeline the sum tree.
    wire signed [PROD_W-1:0]  product [N_TAPS];
    for (genvar i = 0; i < N_TAPS; i++) begin : g_prod
        assign product[i] = sample_sr_next[i] * coef[i];
    end

    // Wide sum: each product is sign-extended to ACCUM_W and added.
    // Synthesisers reduce this to a balanced adder tree.
    function automatic logic signed [ACCUM_W-1:0] sum_products;
        sum_products = '0;
        for (int i = 0; i < N_TAPS; i++) begin
            sum_products = sum_products +
                {{(ACCUM_W-PROD_W){product[i][PROD_W-1]}}, product[i]};
        end
    endfunction

    wire signed [ACCUM_W-1:0] accum = sum_products();

    // Output formation: arithmetic shift right by OUT_SHIFT, then take
    // top OUT_W bits (truncation).
    wire signed [ACCUM_W-1:0] shifted = accum >>> OUT_SHIFT;
    wire signed [OUT_W-1:0]   sample_out_now = shifted[OUT_W-1:0];

    // ====================================================================
    // Output register
    // ====================================================================

    reg signed [OUT_W-1:0] out_data;
    reg                    out_valid;

    assign s_axis_tready = !(out_valid && !m_axis_tready);
    assign m_axis_tdata  = out_data;
    assign m_axis_tvalid = out_valid;

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < N_TAPS; i++) sample_sr[i] <= '0;
            out_data  <= '0;
            out_valid <= 1'b0;
        end else begin
            if (output_handshake) out_valid <= 1'b0;
            if (input_handshake) begin
                // Commit the shift and produce one output sample.
                for (i = 0; i < N_TAPS; i++) sample_sr[i] <= sample_sr_next[i];
                out_data  <= sample_out_now;
                out_valid <= 1'b1;
            end
        end
    end

    // ====================================================================
    // AXI-Lite coefficient bank
    // ====================================================================
    //
    // Single-FSM-per-channel slave. AW/W collect their handshakes
    // independently; once both seen, decode the index, commit the
    // write, drive the B response. Reads symmetric: AR captures the
    // address, R returns the looked-up coefficient.

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

    // Accept inputs unless we have a captured value waiting to commit.
    assign s_axil_awready = !aw_seen_q;
    assign s_axil_wready  = !w_seen_q;
    assign s_axil_bvalid  = b_valid_q;
    assign s_axil_bresp   = b_resp_q;
    assign s_axil_arready = !ar_seen_q;
    assign s_axil_rvalid  = r_valid_q;
    assign s_axil_rdata   = r_data_q;
    assign s_axil_rresp   = r_resp_q;

    // Index decoded from the captured AW or AR address. Word offset =
    // addr[31:2]. We compare against N_TAPS for DECERR.
    wire [29:0] aw_word_idx = aw_addr_q[31:2];
    wire        aw_in_range = (aw_word_idx < 30'(N_TAPS));
    wire [IDX_BITS-1:0] aw_idx = aw_word_idx[IDX_BITS-1:0];

    wire [29:0] ar_word_idx = ar_addr_q[31:2];
    wire        ar_in_range = (ar_word_idx < 30'(N_TAPS));
    wire [IDX_BITS-1:0] ar_idx = ar_word_idx[IDX_BITS-1:0];

    // Coefficient read: sign-extend coef[ar_idx] to 32 bits.
    wire [31:0] coef_read_value =
        ar_in_range ? {{(32 - COEF_W){coef[ar_idx][COEF_W-1]}}, coef[ar_idx]}
                    : 32'h0;

    integer j;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            aw_addr_q  <= '0;
            aw_seen_q  <= 1'b0;
            w_data_q   <= '0;
            w_seen_q   <= 1'b0;
            b_valid_q  <= 1'b0;
            b_resp_q   <= RESP_OKAY;
            ar_addr_q  <= '0;
            ar_seen_q  <= 1'b0;
            r_valid_q  <= 1'b0;
            r_data_q   <= '0;
            r_resp_q   <= RESP_OKAY;
            for (j = 0; j < N_TAPS; j++) coef[j] <= '0;
        end else begin
            // ---- Write path -------------------------------------------------
            if (s_axil_awvalid && s_axil_awready) begin
                aw_addr_q <= s_axil_awaddr;
                aw_seen_q <= 1'b1;
            end
            if (s_axil_wvalid && s_axil_wready) begin
                w_data_q <= s_axil_wdata;
                w_seen_q <= 1'b1;
            end
            // When both AW and W have been seen and no B is pending,
            // commit the write and raise B.
            if (aw_seen_q && w_seen_q && !b_valid_q) begin
                if (aw_in_range) begin
                    coef[aw_idx] <= w_data_q[COEF_W-1:0];
                    b_resp_q <= RESP_OKAY;
                end else begin
                    b_resp_q <= RESP_DECERR;
                end
                b_valid_q <= 1'b1;
                aw_seen_q <= 1'b0;
                w_seen_q  <= 1'b0;
            end
            // B handshake clears.
            if (s_axil_bvalid && s_axil_bready) b_valid_q <= 1'b0;

            // ---- Read path --------------------------------------------------
            if (s_axil_arvalid && s_axil_arready) begin
                ar_addr_q <= s_axil_araddr;
                ar_seen_q <= 1'b1;
            end
            if (ar_seen_q && !r_valid_q) begin
                r_data_q  <= coef_read_value;
                r_resp_q  <= ar_in_range ? RESP_OKAY : RESP_DECERR;
                r_valid_q <= 1'b1;
                ar_seen_q <= 1'b0;
            end
            if (s_axil_rvalid && s_axil_rready) r_valid_q <= 1'b0;
        end
    end

endmodule

`default_nettype wire
