// =============================================================================
// audio_decimator.sv
//
// Audio-rate decimator wrapper for the FM receive chain. Composes one
// cic_decimator and one fir_filter into a single module with one AXIS
// slave, one AXIS master, and one AXI-Lite slave (passed through to
// the FIR's coefficient bank).
//
// Why a wrapper rather than instantiating CIC + FIR directly in
// plover.sv: when the full FM chain lands later, plover.sv will have
// many DSP blocks. Composing CIC+FIR pairs into named "channel
// decimator" and "audio decimator" units keeps the integration step
// legible — there's one named block per logical chain stage, not
// twenty individual sub-instantiations.
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
// The CIC has no AXI-Lite — all its behaviour is parameter-driven.
// The FIR's AXI-Lite is the wrapper's only software-visible state.
//
// Audio-path parameter defaults are chosen for ~250 kHz -> 50 kHz
// decimation: R=5, N=3 stages, 16 FIR taps. Software programs the FIR
// taps for a passband that compensates the CIC droop and rolls off
// above the audio band ceiling.
//
// Bit-exactness
// -------------
// The wrapper is pure plumbing — no arithmetic of its own. The CIC
// and FIR each have their own bit-exact Python models; chaining them
// in the test bench (using the existing CicFirChain in dv/dsp_models.py
// configured for audio-rate parameters) gives a bit-exact reference
// for the wrapper's combined behaviour. The handshake between CIC and
// FIR is the same one validated by the standalone fir_filter tests —
// the FIR is the only one that ever backpressures, and the CIC tolerates
// it correctly.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module audio_decimator #(
    // CIC parameters
    parameter int unsigned CIC_STAGES   = 3,
    parameter int unsigned CIC_DECIM    = 5,
    parameter int unsigned CIC_DELAY    = 1,

    // Sample widths and Q-format (shared between CIC and FIR
    // input/output — both operate at SAMPLE_W signed in Q-format
    // SAMPLE_INT_W.SAMPLE_FRAC_W).
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,

    // FIR parameters
    parameter int unsigned FIR_N_TAPS     = 16,
    parameter int unsigned FIR_COEF_W     = 16,
    parameter int unsigned FIR_COEF_INT_W = 1,
    parameter int unsigned FIR_COEF_FRAC_W= FIR_COEF_W - FIR_COEF_INT_W,
    // FIR output right-shift after accumulate. Default tracks the
    // coefficient fractional-bit count so the Q-position passes
    // through.
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

    // ---- AXIS slave: pre-decimation samples (real-valued, signed) ----
    input  wire signed [SAMPLE_W-1:0]    s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: post-decimation samples (audio rate) ----
    output wire signed [SAMPLE_W-1:0]    m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    // ---- Q-format consistency check ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "audio_decimator: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
        if (FIR_COEF_INT_W + FIR_COEF_FRAC_W != FIR_COEF_W)
            $fatal(1, "audio_decimator: FIR_COEF_INT_W (%0d) + FIR_COEF_FRAC_W (%0d) != FIR_COEF_W (%0d)",
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
        // AXI-Lite coefficient bank — direct pass-through from the
        // wrapper's port. No address translation needed; the FIR's
        // address space is the wrapper's address space.
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
