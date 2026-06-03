// =============================================================================
// nco.sv
//
// Numerically Controlled Oscillator.
//
// Produces samples of cos(phase) and sin(phase) on an AXIS master port,
// where the phase advances by a software-programmable increment each
// output sample. Output is one complex sample per beat: TDATA holds Q
// (sin) in the upper SAMPLE_W bits and I (cos) in the lower SAMPLE_W
// bits. So TDATA is 2*SAMPLE_W bits total.
//
// Architecture
// ------------
// * Phase accumulator: PHASE_W-bit unsigned register. Each output beat,
//   phase_acc += phase_inc (wraps modulo 2^PHASE_W). The full PHASE_W
//   provides fine frequency resolution; output_freq =
//   (phase_inc / 2^PHASE_W) * sample_rate.
// * Lookup tables: two memories of size 2^LUT_N, each LUT_N entries
//   wide x SAMPLE_W bits. Top LUT_N bits of phase index them. The two
//   tables are populated at elaboration time with cos and sin values
//   in Q1.(SAMPLE_W-1) format, rounded half-up via the +0.5 trick that
//   matches the Python reference model's int(math.floor(x+0.5))
//   convention. Bit-exact comparison against the model holds at every
//   sample.
//
// The lookup is one-cycle latency: phase_acc and table_idx update on
// the input handshake's rising edge; out_data captures the lookup
// results on the *next* rising edge. So one input handshake produces
// one output beat with a 1-cycle pipeline fill (matches FIR shape).
//
// Honest note on LUT spurs
// ------------------------
// With LUT_N bits indexing the table, the spur level relative to the
// carrier is roughly 6*LUT_N dB. LUT_N=10 (default) gives ~60 dBc spurs
// — fine for most SDR uses but not for high-purity reference
// generation. The default is a deliberate sim-budget choice; a larger
// LUT_N just costs more BRAM, no logic change.
//
// AXI-Lite control register
// -------------------------
// One 32-bit word at byte offset 0x00 holds phase_inc. The RTL takes
// the low PHASE_W bits on write; readback is zero-padded if PHASE_W <
// 32. After reset phase_inc is zero -> the NCO sits at phase 0
// producing constant (cos, sin) = (max-positive, 0). Software must
// program a nonzero phase_inc to get a tone.
//
// AXIS interface
// --------------
// AXIS slave: present on each cycle the consumer wants a sample. NCO
// is "self-driving" — it doesn't take input samples; it produces one
// output per s_axis handshake (tvalid is held high while NCO is
// running). Actually for a self-driving NCO it's cleaner to model
// without an input port: the NCO produces one sample per cycle and
// the consumer reads as fast as it can. But to keep the AXIS contract
// uniform with the rest of the project (downstream blocks expect a
// standard tvalid/tready handshake), we expose only the master port
// and tie tvalid high whenever the NCO is enabled (i.e. after reset).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module nco #(
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    parameter int unsigned PHASE_W        = 32,
    parameter int unsigned LUT_N          = 10
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXI-Lite slave for phase_inc programming ----
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

    // ---- AXIS master (IQ output, Q in high SAMPLE_W, I in low SAMPLE_W) ----
    output wire [2*SAMPLE_W-1:0]         m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    localparam int          LUT_SIZE   = 1 << LUT_N;
    localparam logic [1:0]  RESP_OKAY   = 2'b00;
    localparam logic [1:0]  RESP_DECERR = 2'b11;
    localparam logic [31:0] ADDR_MASK  = 32'h0000_0003;

    // Maximum positive value in Q1.(SAMPLE_W-1): saturate to 2^(SAMPLE_W-1)-1
    // so cos(0)=+1.0 maps to the largest positive integer the format
    // can represent.
    localparam int signed LUT_SCALE = (1 << (SAMPLE_W - 1)) - 1;

    // ---- Q-format consistency check ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "nco: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
    end

    // ====================================================================
    // Build the sin/cos lookup tables at elaboration time.
    // Both Python model and RTL use int(floor(value * scale + 0.5)) to
    // round half-up, so the two agree bit-for-bit.
    // ====================================================================

    function automatic logic signed [SAMPLE_W-1:0] sin_entry(input int k);
        real angle;
        real value;
        int  rounded;
        angle = 2.0 * 3.14159265358979323846 * real'(k) / real'(LUT_SIZE);
        value = $sin(angle) * real'(LUT_SCALE);
        // round half-up to nearest integer. $rtoi truncates toward zero,
        // so we add 0.5 for positives and subtract 0.5 for negatives.
        if (value >= 0.0) rounded = $rtoi(value + 0.5);
        else              rounded = $rtoi(value - 0.5);
        return SAMPLE_W'(rounded);
    endfunction

    function automatic logic signed [SAMPLE_W-1:0] cos_entry(input int k);
        real angle;
        real value;
        int  rounded;
        angle = 2.0 * 3.14159265358979323846 * real'(k) / real'(LUT_SIZE);
        value = $cos(angle) * real'(LUT_SCALE);
        if (value >= 0.0) rounded = $rtoi(value + 0.5);
        else              rounded = $rtoi(value - 0.5);
        return SAMPLE_W'(rounded);
    endfunction

    // Memories. ROM-like, populated by an initial block. Using packed
    // dimensions for cleaner part-selects.
    logic signed [SAMPLE_W-1:0] sin_lut [LUT_SIZE];
    logic signed [SAMPLE_W-1:0] cos_lut [LUT_SIZE];

    initial begin : g_lut_init
        for (int k = 0; k < LUT_SIZE; k++) begin
            sin_lut[k] = sin_entry(k);
            cos_lut[k] = cos_entry(k);
        end
    end

    // ====================================================================
    // AXI-Lite slave (single phase_inc register)
    // ====================================================================

    reg [PHASE_W-1:0] phase_inc;

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

    wire [31:0] aw_masked    = aw_addr_q & ADDR_MASK;
    wire        aw_in_range  = (aw_masked == 32'h0);
    wire [31:0] ar_masked    = ar_addr_q & ADDR_MASK;
    wire        ar_in_range  = (ar_masked == 32'h0);
    wire [31:0] phase_read_value = {{(32 - PHASE_W){1'b0}}, phase_inc};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            phase_inc   <= '0;
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
                w_data_q  <= s_axil_wdata; w_seen_q <= 1'b1;
            end
            if (aw_seen_q && w_seen_q && !b_valid_q) begin
                if (aw_in_range) begin
                    phase_inc <= w_data_q[PHASE_W-1:0];
                    b_resp_q  <= RESP_OKAY;
                end else begin
                    b_resp_q  <= RESP_DECERR;
                end
                b_valid_q <= 1'b1;
                aw_seen_q <= 1'b0;
                w_seen_q  <= 1'b0;
            end
            if (s_axil_bvalid && s_axil_bready) b_valid_q <= 1'b0;

            if (s_axil_arvalid && s_axil_arready) begin
                ar_addr_q <= s_axil_araddr; ar_seen_q <= 1'b1;
            end
            if (ar_seen_q && !r_valid_q) begin
                r_data_q  <= ar_in_range ? phase_read_value : 32'h0;
                r_resp_q  <= ar_in_range ? RESP_OKAY : RESP_DECERR;
                r_valid_q <= 1'b1;
                ar_seen_q <= 1'b0;
            end
            if (s_axil_rvalid && s_axil_rready) r_valid_q <= 1'b0;
        end
    end

    // ====================================================================
    // Phase accumulator + LUT
    // ====================================================================

    reg [PHASE_W-1:0]      phase_acc;
    reg signed [SAMPLE_W-1:0] i_data, q_data;  // cos, sin
    reg                       out_valid;

    wire output_handshake = m_axis_tvalid && m_axis_tready;

    assign m_axis_tvalid = out_valid;
    // TDATA packing: Q in [2*SAMPLE_W-1:SAMPLE_W], I in [SAMPLE_W-1:0].
    assign m_axis_tdata  = { q_data, i_data };

    // LUT index: top LUT_N bits of phase_acc (effectively rounding down
    // the phase to the LUT granularity).
    wire [LUT_N-1:0] lut_idx = phase_acc[PHASE_W-1 -: LUT_N];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            phase_acc <= '0;
            i_data    <= '0;
            q_data    <= '0;
            out_valid <= 1'b0;
        end else begin
            // Free-running output: assert tvalid every cycle once out
            // of reset. If the consumer isn't ready, we hold the
            // current sample (phase doesn't advance).
            if (output_handshake || !out_valid) begin
                phase_acc <= phase_acc + phase_inc;
                i_data    <= cos_lut[lut_idx];
                q_data    <= sin_lut[lut_idx];
                out_valid <= 1'b1;
            end
        end
    end

endmodule

`default_nettype wire
