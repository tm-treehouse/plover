// =============================================================================
// channel_decimator.sv
//
// Channel-rate decimator wrapper. Composes cic_decimator + fir_filter with
// audio-decimator-style packaging: one AXIS in, one AXIS out, one AXI-Lite
// passed through to the FIR coefficient bank. Pure plumbing — no new
// arithmetic.
//
// Sibling of audio_decimator. The difference is parameter defaults: this
// unit's defaults target the *channel* decimation step in the FM receive
// chain (high baseband rate -> intermediate chain rate, roughly 10x),
// while audio_decimator targets the audio step (intermediate -> audio,
// roughly 5x).
//
//   Default channel chain: CIC R=10, N=3, M=1; FIR 32 taps
//   Default audio chain:   CIC R=5,  N=3, M=1; FIR 16 taps
//
// Why a separate unit rather than parameterising audio_decimator: the
// defaults are different and the address-map intent is different
// (channel decim is the front-end's channel-select filter; audio decim
// is the back-end's anti-alias). Distinct names make the front-end
// integration RTL read like the signal flow rather than like a sequence
// of identical instances.
//
// Structure
// ---------
//
//   s_axis_*  (SAMPLE_W signed, in-rate)
//     -> cic_decimator (R, N stages, M=DELAY)
//     -> intermediate AXIS (SAMPLE_W signed, in-rate / R)
//     -> fir_filter (N_TAPS taps, software-programmable coefs)
//     -> m_axis_*  (SAMPLE_W signed, in-rate / R)
//
//   s_axil_*  -> fir_filter's AXI-Lite (coefficient bank)
//
// The CIC has no AXI-Lite — all parameter-driven. The FIR's AXI-Lite is
// the wrapper's only software-visible state, passed through directly.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module channel_decimator #(
    // CIC parameters — channel-rate defaults (heavier decimation than
    // audio_decimator).
    parameter int unsigned CIC_STAGES   = 3,
    parameter int unsigned CIC_DECIM    = 10,
    parameter int unsigned CIC_DELAY    = 1,

    // Sample widths.
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,

    // FIR parameters — more taps than audio_decimator for better
    // adjacent-channel rejection in the channel-select stage.
    parameter int unsigned FIR_N_TAPS     = 32,
    parameter int unsigned FIR_COEF_W     = 16,
    parameter int unsigned FIR_COEF_INT_W = 1,
    parameter int unsigned FIR_COEF_FRAC_W= FIR_COEF_W - FIR_COEF_INT_W,
    parameter int          FIR_OUT_SHIFT  = FIR_COEF_FRAC_W
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXI-Lite slave (passed through to FIR) ----
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

    // ---- AXIS slave: pre-decimation samples ----
    input  wire signed [SAMPLE_W-1:0]    s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: channel-rate samples ----
    output wire signed [SAMPLE_W-1:0]    m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "channel_decimator: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
        if (FIR_COEF_INT_W + FIR_COEF_FRAC_W != FIR_COEF_W)
            $fatal(1, "channel_decimator: FIR_COEF_INT_W (%0d) + FIR_COEF_FRAC_W (%0d) != FIR_COEF_W (%0d)",
                   FIR_COEF_INT_W, FIR_COEF_FRAC_W, FIR_COEF_W);
    end

    // ---- CIC -> FIR intermediate AXIS ----
    wire signed [SAMPLE_W-1:0] cic2fir_tdata;
    wire                       cic2fir_tvalid;
    wire                       cic2fir_tready;

    cic_decimator #(
        .STAGES     (CIC_STAGES),
        .DECIM      (CIC_DECIM),
        .DELAY      (CIC_DELAY),
        .IN_W       (SAMPLE_W),
        .IN_INT_W   (SAMPLE_INT_W),
        .IN_FRAC_W  (SAMPLE_FRAC_W),
        .OUT_W      (SAMPLE_W),
        .OUT_INT_W  (SAMPLE_INT_W),
        .OUT_FRAC_W (SAMPLE_FRAC_W)
    ) u_cic (
        .clk(clk), .rst_n(rst_n),
        .s_axis_tdata  (s_axis_tdata),
        .s_axis_tvalid (s_axis_tvalid),
        .s_axis_tready (s_axis_tready),
        .m_axis_tdata  (cic2fir_tdata),
        .m_axis_tvalid (cic2fir_tvalid),
        .m_axis_tready (cic2fir_tready)
    );

    fir_filter #(
        .N_TAPS      (FIR_N_TAPS),
        .IN_W        (SAMPLE_W),
        .IN_INT_W    (SAMPLE_INT_W),
        .IN_FRAC_W   (SAMPLE_FRAC_W),
        .COEF_W      (FIR_COEF_W),
        .COEF_INT_W  (FIR_COEF_INT_W),
        .COEF_FRAC_W (FIR_COEF_FRAC_W),
        .OUT_W       (SAMPLE_W),
        .OUT_INT_W   (SAMPLE_INT_W),
        .OUT_FRAC_W  (SAMPLE_FRAC_W),
        .OUT_SHIFT   (FIR_OUT_SHIFT)
    ) u_fir (
        .clk(clk), .rst_n(rst_n),
        // AXI-Lite passed through directly.
        .s_axil_awaddr (s_axil_awaddr),
        .s_axil_awprot (s_axil_awprot),
        .s_axil_awvalid(s_axil_awvalid),
        .s_axil_awready(s_axil_awready),
        .s_axil_wdata  (s_axil_wdata),
        .s_axil_wstrb  (s_axil_wstrb),
        .s_axil_wvalid (s_axil_wvalid),
        .s_axil_wready (s_axil_wready),
        .s_axil_bresp  (s_axil_bresp),
        .s_axil_bvalid (s_axil_bvalid),
        .s_axil_bready (s_axil_bready),
        .s_axil_araddr (s_axil_araddr),
        .s_axil_arprot (s_axil_arprot),
        .s_axil_arvalid(s_axil_arvalid),
        .s_axil_arready(s_axil_arready),
        .s_axil_rdata  (s_axil_rdata),
        .s_axil_rresp  (s_axil_rresp),
        .s_axil_rvalid (s_axil_rvalid),
        .s_axil_rready (s_axil_rready),
        // Sample AXIS
        .s_axis_tdata  (cic2fir_tdata),
        .s_axis_tvalid (cic2fir_tvalid),
        .s_axis_tready (cic2fir_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready)
    );

endmodule

`default_nettype wire
