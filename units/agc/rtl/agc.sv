// =============================================================================
// agc.sv
//
// Automatic gain control. Scales a complex AXIS-IQ stream so the
// observed magnitude tracks a software-programmable target. The first
// closed-loop feedback unit in the project — be careful with the gain
// register's update timing, because any disagreement between RTL and
// Python model compounds into runaway divergence (the gain state's
// trajectory depends on its own history).
//
// Architecture
// ------------
// Per output beat:
//
//   1. magnitude = |in.I| + |in.Q|              -- cheap approximation
//   2. error     = target - magnitude            -- signed
//   3. delta     = (error * 1) >>> mu_shift     -- loop step is 2^-mu_shift
//   4. gain_next = clamp(gain + delta, gain_min, gain_max)
//   5. out.I     = (in.I * gain) >>> GAIN_FRAC_W
//   6. out.Q     = (in.Q * gain) >>> GAIN_FRAC_W
//
// Step 5/6 uses the *current* (pre-update) value of `gain`. The new
// `gain_next` is written to the register on the same clock edge that
// the output beat is emitted — it's used next cycle. The Python model
// mirrors this exactly: read gain, multiply, then update gain.
//
// Magnitude estimate
// ------------------
// |I| + |Q| is fast and cheap but biased high by up to 1/cos(45 deg) ~
// 1.414x for a 45-deg-rotated signal. For FM broadcast where the
// modulation is *constant envelope*, the magnitude is approximately
// constant regardless of estimator — the bias just shifts the
// effective target. A more accurate estimator (true sqrt via CORDIC,
// or the alpha-max-beta-min approximation) is a future improvement.
//
// Gain Q-format
// -------------
// Default Q4.12: 1 sign-equivalent bit (gain is unsigned but we use
// signed math internally) plus 3 integer bits plus 12 fractional bits.
// Range: 0 to ~16.0, resolution ~244 ppm. Roughly +24 dB peak gain.
// For wider dynamic range, increase GAIN_INT_W (and matching widths
// of gain_min/max/init registers).
//
// AXI-Lite register map
// ---------------------
//   0x00  target          (SAMPLE_W bits, signed; Q-aligned with samples)
//   0x04  mu_shift        (5 bits; loop step = 2^-mu_shift)
//   0x08  gain_min        (GAIN_W bits, unsigned Q-format)
//   0x0C  gain_max        (GAIN_W bits, unsigned Q-format)
//   0x10  gain_init       (GAIN_W bits, unsigned Q-format)
//   0x14  control         (bit 0 = reset_gain pulse; self-clearing)
//   0x18  gain_observed   (read-only; mirrors current gain register)
//
// After reset, gain is set to gain_init (which itself resets to
// GAIN_DEFAULT = 1.0 << GAIN_FRAC_W).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module agc #(
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    parameter int unsigned GAIN_W         = 16,
    parameter int unsigned GAIN_INT_W     = 4,
    parameter int unsigned GAIN_FRAC_W    = GAIN_W - GAIN_INT_W
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

    // ---- AXIS slave: complex input ----
    input  wire [2*SAMPLE_W-1:0]         s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: complex output ----
    output wire [2*SAMPLE_W-1:0]         m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    // ---- Q-format consistency checks ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "agc: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
        if (GAIN_INT_W + GAIN_FRAC_W != GAIN_W)
            $fatal(1, "agc: GAIN_INT_W (%0d) + GAIN_FRAC_W (%0d) != GAIN_W (%0d)",
                   GAIN_INT_W, GAIN_FRAC_W, GAIN_W);
    end

    // Default gain register value at reset: 1.0 in Q-format = 1 << GAIN_FRAC_W.
    localparam logic [GAIN_W-1:0] GAIN_DEFAULT = GAIN_W'(1) << GAIN_FRAC_W;

    localparam logic [1:0]  RESP_OKAY   = 2'b00;
    localparam logic [1:0]  RESP_DECERR = 2'b11;
    // 7 registers -> need 5-bit byte address (offsets 0x00..0x18). Mask
    // to a 3-bit word-index range for decoder simplicity.
    localparam logic [31:0] ADDR_MASK   = 32'h0000_001F;

    // ====================================================================
    // AXI-Lite slave
    // ====================================================================

    reg signed [SAMPLE_W-1:0] target;
    reg [4:0]                 mu_shift;
    reg [GAIN_W-1:0]          gain_min;
    reg [GAIN_W-1:0]          gain_max;
    reg [GAIN_W-1:0]          gain_init;
    reg                       reset_gain_pulse;

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

    // Read-only mirror of current gain (declared early so it's in scope
    // for the AXI-Lite read path).
    reg [GAIN_W-1:0] gain;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            target          <= '0;
            mu_shift        <= 5'd14;          // mu = 2^-14, fast for tests
            gain_min        <= '0;
            gain_max        <= '1;             // unclamped by default
            gain_init       <= GAIN_DEFAULT;
            reset_gain_pulse <= 1'b0;
            aw_addr_q   <= '0; aw_seen_q <= 1'b0;
            w_data_q    <= '0; w_seen_q  <= 1'b0;
            b_valid_q   <= 1'b0; b_resp_q <= RESP_OKAY;
            ar_addr_q   <= '0; ar_seen_q <= 1'b0;
            r_valid_q   <= 1'b0; r_data_q <= '0; r_resp_q <= RESP_OKAY;
        end else begin
            // reset_gain_pulse is one-shot: clears next cycle.
            reset_gain_pulse <= 1'b0;

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
                case (aw_masked[4:2])
                    3'h0: target    <= w_data_q[SAMPLE_W-1:0];
                    3'h1: mu_shift  <= w_data_q[4:0];
                    3'h2: gain_min  <= w_data_q[GAIN_W-1:0];
                    3'h3: gain_max  <= w_data_q[GAIN_W-1:0];
                    3'h4: gain_init <= w_data_q[GAIN_W-1:0];
                    3'h5: reset_gain_pulse <= w_data_q[0];
                    // 3'h6 (gain_observed) is read-only; ignore writes.
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
                case (ar_masked[4:2])
                    3'h0: r_data_q <= {{(32-SAMPLE_W){target[SAMPLE_W-1]}}, target};
                    3'h1: r_data_q <= {27'h0, mu_shift};
                    3'h2: r_data_q <= {{(32-GAIN_W){1'b0}}, gain_min};
                    3'h3: r_data_q <= {{(32-GAIN_W){1'b0}}, gain_max};
                    3'h4: r_data_q <= {{(32-GAIN_W){1'b0}}, gain_init};
                    3'h5: r_data_q <= 32'h0;       // pulse always reads 0
                    3'h6: r_data_q <= {{(32-GAIN_W){1'b0}}, gain};
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
    // AGC datapath
    // ====================================================================

    // Unpack input.
    wire signed [SAMPLE_W-1:0] in_i = s_axis_tdata[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0] in_q = s_axis_tdata[2*SAMPLE_W-1:SAMPLE_W];

    // Magnitude estimate: |I| + |Q|. Both operands fit in SAMPLE_W bits
    // unsigned (abs has range 0..2^(SAMPLE_W-1)); sum needs SAMPLE_W bits.
    wire [SAMPLE_W-1:0] abs_i = in_i[SAMPLE_W-1] ? (~in_i + 1'b1) : in_i;
    wire [SAMPLE_W-1:0] abs_q = in_q[SAMPLE_W-1] ? (~in_q + 1'b1) : in_q;
    wire [SAMPLE_W:0]   magnitude = abs_i + abs_q;            // SAMPLE_W+1 bits

    // Error: target (signed SAMPLE_W) - magnitude (unsigned SAMPLE_W+1).
    // To compare in a common signed space: sign-extend target to
    // SAMPLE_W+2, zero-extend magnitude to SAMPLE_W+2, subtract.
    wire signed [SAMPLE_W+1:0] target_se     = {{2{target[SAMPLE_W-1]}}, target};
    wire signed [SAMPLE_W+1:0] magnitude_se  = {1'b0, magnitude};
    wire signed [SAMPLE_W+1:0] error         = target_se - magnitude_se;

    // Loop step: arithmetic shift right by mu_shift. Result is the
    // signed delta added to the unsigned gain register.
    wire signed [SAMPLE_W+1:0] delta         = error >>> mu_shift;

    // gain_next: clamped gain + delta. Use a wider signed adder so the
    // sum can overflow gracefully before clamping. The widest case
    // needs GAIN_W+1 bits signed (delta can be negative and larger
    // than current gain).
    wire signed [GAIN_W+1:0] gain_signed  = {2'b00, gain};
    wire signed [GAIN_W+1:0] delta_widened = (delta[SAMPLE_W+1])
        ? {{(GAIN_W-SAMPLE_W){1'b1}}, delta}    // sign-extend negative
        : {{(GAIN_W-SAMPLE_W){1'b0}}, delta};   // zero-extend non-negative

    wire signed [GAIN_W+1:0] gain_summed   = gain_signed + delta_widened;
    wire signed [GAIN_W+1:0] gain_min_se   = {2'b00, gain_min};
    wire signed [GAIN_W+1:0] gain_max_se   = {2'b00, gain_max};

    wire below_min = (gain_summed < gain_min_se);
    wire above_max = (gain_summed > gain_max_se);
    wire [GAIN_W-1:0] gain_next = below_min ? gain_min :
                                  above_max ? gain_max :
                                  gain_summed[GAIN_W-1:0];

    // Apply current gain to input. The multiplier output is
    // SAMPLE_W + GAIN_W bits signed (gain is unsigned but mathematically
    // non-negative so a wider signed product is safe). Shift right by
    // GAIN_FRAC_W to preserve the input's Q-position.
    wire signed [SAMPLE_W+GAIN_W-1:0] prod_i_full = $signed({1'b0, gain}) * in_i;
    wire signed [SAMPLE_W+GAIN_W-1:0] prod_q_full = $signed({1'b0, gain}) * in_q;
    wire signed [SAMPLE_W+GAIN_W-1:0] shifted_i   = prod_i_full >>> GAIN_FRAC_W;
    wire signed [SAMPLE_W+GAIN_W-1:0] shifted_q   = prod_q_full >>> GAIN_FRAC_W;
    wire signed [SAMPLE_W-1:0]        out_i_now   = shifted_i[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0]        out_q_now   = shifted_q[SAMPLE_W-1:0];

    // ---- Handshake + state update ----
    wire input_fire = s_axis_tvalid && s_axis_tready;

    reg signed [SAMPLE_W-1:0] out_i_q, out_q_q;
    reg                       out_valid_q;

    assign s_axis_tready = !out_valid_q || m_axis_tready;
    assign m_axis_tvalid = out_valid_q;
    assign m_axis_tdata  = { out_q_q, out_i_q };

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            gain        <= GAIN_DEFAULT;       // gain_init not yet user-set
            out_i_q     <= '0;
            out_q_q     <= '0;
            out_valid_q <= 1'b0;
        end else begin
            if (reset_gain_pulse) begin
                gain <= gain_init;
            end else if (input_fire) begin
                // Update gain to gain_next; this same cycle's output
                // beat used the *current* (pre-update) gain, captured
                // into out_i_q/out_q_q on this edge.
                gain    <= gain_next;
                out_i_q <= out_i_now;
                out_q_q <= out_q_now;
                out_valid_q <= 1'b1;
            end else if (m_axis_tready && out_valid_q) begin
                out_valid_q <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
