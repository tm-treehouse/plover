// =============================================================================
// fm_demod.sv
//
// FM broadcast demodulator chain — first multi-unit RTL integration.
//
// Wires the four demod-side DSP units into one module:
//
//     AXIS IQ in
//       -> cordic            (16-stage vectoring)
//       -> phase_diff         (instantaneous frequency, implicit unwrap)
//       -> deemphasis         (single-pole IIR low-pass)
//       -> audio_decimator    (CIC + FIR, ~5x decimation)
//       -> AXIS audio out
//
// An internal axil_xbar fans the single AXI-Lite slave port out to the
// two register-banked sub-units (deemphasis and audio_decimator).
// CORDIC and phase_diff have no AXI-Lite — they're datapath only.
//
// Address map (4 KB pages, matching plover.sv's main-xbar convention):
//
//     0x0000_0000 .. 0x0000_0FFF  -> deemphasis      (alpha at 0x00)
//     0x0000_1000 .. 0x0000_1FFF  -> audio_decimator (FIR coefs at 0x00..N_TAPS*4)
//
// TDATA inter-unit plumbing
// -------------------------
// CORDIC output is { padding, phase[PHASE_W-1:0], magnitude[SAMPLE_W+1:0] }
// byte-aligned to TDATA_W bits. Only the phase slice is consumed; the
// magnitude (and any padding bits) are dropped. PhaseDiff input is just
// the phase bits.
//
// Magnitude could be used downstream for signal-quality metrics (RSSI,
// SNR estimation, lock-loss detection). Not wired here — the FM demod
// chain is phase-only. A future commit can add a magnitude side-port if
// the project grows a signal-quality feature.
//
// Scope of this commit
// --------------------
// This commit lands the *wiring*: the module compiles, lints clean, and
// passes one trivial smoke test that drives a few IQ samples and
// observes audio output (any output — bit-exact comparison comes in a
// later commit). The methodology pattern from per-unit commits is more
// invasive at the integration level; splitting "compiles + produces
// output" from "matches the reference model bit-exactly" makes each
// commit reviewable in isolation.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module fm_demod #(
    // Sample widths — shared across the chain.
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    parameter int unsigned PHASE_W        = 16,
    parameter int unsigned CORDIC_ITER    = 16,

    // Deemphasis coefficient widths.
    parameter int unsigned DEEMPH_COEF_W      = 16,
    parameter int unsigned DEEMPH_COEF_INT_W  = 1,
    parameter int unsigned DEEMPH_COEF_FRAC_W = DEEMPH_COEF_W - DEEMPH_COEF_INT_W,

    // Audio decimator parameters.
    parameter int unsigned AUDIO_CIC_STAGES   = 3,
    parameter int unsigned AUDIO_CIC_DECIM    = 5,
    parameter int unsigned AUDIO_CIC_DELAY    = 1,
    parameter int unsigned AUDIO_FIR_N_TAPS   = 16,
    parameter int unsigned AUDIO_FIR_COEF_W   = 16,
    parameter int unsigned AUDIO_FIR_COEF_INT_W  = 1,
    parameter int unsigned AUDIO_FIR_COEF_FRAC_W = AUDIO_FIR_COEF_W - AUDIO_FIR_COEF_INT_W,
    parameter int          AUDIO_FIR_OUT_SHIFT   = AUDIO_FIR_COEF_FRAC_W
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXI-Lite slave (single port; internally fanned out) ----
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

    // ---- AXIS slave: complex IQ in ----
    input  wire [2*SAMPLE_W-1:0]         s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: real-valued audio out ----
    output wire signed [SAMPLE_W-1:0]    m_axis_tdata,
    output wire                          m_axis_tvalid,
    input  wire                          m_axis_tready
);

    // -----------------------------------------------------------------
    // Internal AXI-Lite xbar (2 slaves)
    // -----------------------------------------------------------------

    localparam int N_AXIL_SLAVES = 2;
    localparam int DEEMPH_IDX    = 0;
    localparam int AUDEC_IDX     = 1;

    // Slave base/mask — 4 KB pages. Indices match DEEMPH_IDX=0,
    // AUDEC_IDX=1; positional initialization avoids Verilator's
    // limitation on named-key assignment patterns with localparam keys.
    localparam logic [31:0] AXIL_SLAVE_BASE [N_AXIL_SLAVES] = '{
        32'h0000_0000,    // DEEMPH_IDX
        32'h0000_1000     // AUDEC_IDX
    };
    localparam logic [31:0] AXIL_SLAVE_MASK [N_AXIL_SLAVES] = '{
        default: 32'hFFFF_F000
    };

    wire [N_AXIL_SLAVES*32-1:0] m_axil_awaddr;
    wire [N_AXIL_SLAVES*3-1:0]  m_axil_awprot;
    wire [N_AXIL_SLAVES-1:0]    m_axil_awvalid;
    wire [N_AXIL_SLAVES-1:0]    m_axil_awready;
    wire [N_AXIL_SLAVES*32-1:0] m_axil_wdata;
    wire [N_AXIL_SLAVES*4-1:0]  m_axil_wstrb;
    wire [N_AXIL_SLAVES-1:0]    m_axil_wvalid;
    wire [N_AXIL_SLAVES-1:0]    m_axil_wready;
    wire [N_AXIL_SLAVES*2-1:0]  m_axil_bresp;
    wire [N_AXIL_SLAVES-1:0]    m_axil_bvalid;
    wire [N_AXIL_SLAVES-1:0]    m_axil_bready;
    wire [N_AXIL_SLAVES*32-1:0] m_axil_araddr;
    wire [N_AXIL_SLAVES*3-1:0]  m_axil_arprot;
    wire [N_AXIL_SLAVES-1:0]    m_axil_arvalid;
    wire [N_AXIL_SLAVES-1:0]    m_axil_arready;
    wire [N_AXIL_SLAVES*32-1:0] m_axil_rdata;
    wire [N_AXIL_SLAVES*2-1:0]  m_axil_rresp;
    wire [N_AXIL_SLAVES-1:0]    m_axil_rvalid;
    wire [N_AXIL_SLAVES-1:0]    m_axil_rready;

    axil_xbar #(
        .N_SLAVES   (N_AXIL_SLAVES),
        .ADDR_WIDTH (32),
        .DATA_WIDTH (32),
        .SLAVE_BASE (AXIL_SLAVE_BASE),
        .SLAVE_MASK (AXIL_SLAVE_MASK)
    ) u_xbar (
        .clk(clk), .rst_n(rst_n),
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
        .m_axil_awaddr (m_axil_awaddr),
        .m_axil_awprot (m_axil_awprot),
        .m_axil_awvalid(m_axil_awvalid),
        .m_axil_awready(m_axil_awready),
        .m_axil_wdata  (m_axil_wdata),
        .m_axil_wstrb  (m_axil_wstrb),
        .m_axil_wvalid (m_axil_wvalid),
        .m_axil_wready (m_axil_wready),
        .m_axil_bresp  (m_axil_bresp),
        .m_axil_bvalid (m_axil_bvalid),
        .m_axil_bready (m_axil_bready),
        .m_axil_araddr (m_axil_araddr),
        .m_axil_arprot (m_axil_arprot),
        .m_axil_arvalid(m_axil_arvalid),
        .m_axil_arready(m_axil_arready),
        .m_axil_rdata  (m_axil_rdata),
        .m_axil_rresp  (m_axil_rresp),
        .m_axil_rvalid (m_axil_rvalid),
        .m_axil_rready (m_axil_rready)
    );

    // -----------------------------------------------------------------
    // Inter-unit AXIS wires
    // -----------------------------------------------------------------

    // CORDIC output width: byte-aligned PHASE_W + (SAMPLE_W+2) magnitude.
    localparam int CORDIC_RAW_W  = PHASE_W + SAMPLE_W + 2;
    localparam int CORDIC_TDATA_W = ((CORDIC_RAW_W + 7) / 8) * 8;

    wire [CORDIC_TDATA_W-1:0]    cordic2pd_tdata;
    wire                         cordic2pd_tvalid;
    wire                         cordic2pd_tready;

    // PhaseDiff carries PHASE_W bits.
    wire [PHASE_W-1:0]           pd2de_tdata;
    wire                         pd2de_tvalid;
    wire                         pd2de_tready;

    // Deemphasis -> audio_decimator: SAMPLE_W signed.
    wire [SAMPLE_W-1:0]          de2ad_tdata;
    wire                         de2ad_tvalid;
    wire                         de2ad_tready;

    // -----------------------------------------------------------------
    // CORDIC: IQ -> { phase, magnitude }
    // -----------------------------------------------------------------

    cordic #(
        .SAMPLE_W      (SAMPLE_W),
        .SAMPLE_INT_W  (SAMPLE_INT_W),
        .SAMPLE_FRAC_W (SAMPLE_FRAC_W),
        .PHASE_W       (PHASE_W),
        .ITERATIONS    (CORDIC_ITER)
    ) u_cordic (
        .clk(clk), .rst_n(rst_n),
        .s_axis_tdata  (s_axis_tdata),
        .s_axis_tvalid (s_axis_tvalid),
        .s_axis_tready (s_axis_tready),
        .m_axis_tdata  (cordic2pd_tdata),
        .m_axis_tvalid (cordic2pd_tvalid),
        .m_axis_tready (cordic2pd_tready)
    );

    // Slice the phase out of CORDIC's TDATA. Layout (low-bit-first):
    //   [SAMPLE_W+1 : 0]                 magnitude (dropped)
    //   [SAMPLE_W+PHASE_W+1 : SAMPLE_W+2] phase    (kept)
    //   [TDATA_W-1 : SAMPLE_W+PHASE_W+2] padding   (dropped)
    wire [PHASE_W-1:0] cordic_phase = cordic2pd_tdata[SAMPLE_W+PHASE_W+1 : SAMPLE_W+2];

    // -----------------------------------------------------------------
    // PhaseDiff: phase -> instantaneous frequency
    // -----------------------------------------------------------------

    phase_diff #(
        .PHASE_W (PHASE_W)
    ) u_pd (
        .clk(clk), .rst_n(rst_n),
        .s_axis_tdata  (cordic_phase),
        .s_axis_tvalid (cordic2pd_tvalid),
        .s_axis_tready (cordic2pd_tready),
        .m_axis_tdata  (pd2de_tdata),
        .m_axis_tvalid (pd2de_tvalid),
        .m_axis_tready (pd2de_tready)
    );

    // -----------------------------------------------------------------
    // Deemphasis: low-pass on the frequency stream
    // -----------------------------------------------------------------

    deemphasis #(
        .SAMPLE_W      (SAMPLE_W),
        .SAMPLE_INT_W  (SAMPLE_INT_W),
        .SAMPLE_FRAC_W (SAMPLE_FRAC_W),
        .COEF_W        (DEEMPH_COEF_W),
        .COEF_INT_W    (DEEMPH_COEF_INT_W),
        .COEF_FRAC_W   (DEEMPH_COEF_FRAC_W)
    ) u_de (
        .clk(clk), .rst_n(rst_n),
        // AXI-Lite slave port DEEMPH_IDX
        .s_axil_awaddr (m_axil_awaddr [DEEMPH_IDX*32 +: 32]),
        .s_axil_awprot (m_axil_awprot [DEEMPH_IDX*3  +: 3]),
        .s_axil_awvalid(m_axil_awvalid[DEEMPH_IDX]),
        .s_axil_awready(m_axil_awready[DEEMPH_IDX]),
        .s_axil_wdata  (m_axil_wdata  [DEEMPH_IDX*32 +: 32]),
        .s_axil_wstrb  (m_axil_wstrb  [DEEMPH_IDX*4  +: 4]),
        .s_axil_wvalid (m_axil_wvalid [DEEMPH_IDX]),
        .s_axil_wready (m_axil_wready [DEEMPH_IDX]),
        .s_axil_bresp  (m_axil_bresp  [DEEMPH_IDX*2  +: 2]),
        .s_axil_bvalid (m_axil_bvalid [DEEMPH_IDX]),
        .s_axil_bready (m_axil_bready [DEEMPH_IDX]),
        .s_axil_araddr (m_axil_araddr [DEEMPH_IDX*32 +: 32]),
        .s_axil_arprot (m_axil_arprot [DEEMPH_IDX*3  +: 3]),
        .s_axil_arvalid(m_axil_arvalid[DEEMPH_IDX]),
        .s_axil_arready(m_axil_arready[DEEMPH_IDX]),
        .s_axil_rdata  (m_axil_rdata  [DEEMPH_IDX*32 +: 32]),
        .s_axil_rresp  (m_axil_rresp  [DEEMPH_IDX*2  +: 2]),
        .s_axil_rvalid (m_axil_rvalid [DEEMPH_IDX]),
        .s_axil_rready (m_axil_rready [DEEMPH_IDX]),
        // AXIS
        .s_axis_tdata  (pd2de_tdata),
        .s_axis_tvalid (pd2de_tvalid),
        .s_axis_tready (pd2de_tready),
        .m_axis_tdata  (de2ad_tdata),
        .m_axis_tvalid (de2ad_tvalid),
        .m_axis_tready (de2ad_tready)
    );

    // -----------------------------------------------------------------
    // Audio decimator: 5x decimation + FIR shaping
    // -----------------------------------------------------------------

    audio_decimator #(
        .CIC_STAGES       (AUDIO_CIC_STAGES),
        .CIC_DECIM        (AUDIO_CIC_DECIM),
        .CIC_DELAY        (AUDIO_CIC_DELAY),
        .SAMPLE_W         (SAMPLE_W),
        .SAMPLE_INT_W     (SAMPLE_INT_W),
        .SAMPLE_FRAC_W    (SAMPLE_FRAC_W),
        .FIR_N_TAPS       (AUDIO_FIR_N_TAPS),
        .FIR_COEF_W       (AUDIO_FIR_COEF_W),
        .FIR_COEF_INT_W   (AUDIO_FIR_COEF_INT_W),
        .FIR_COEF_FRAC_W  (AUDIO_FIR_COEF_FRAC_W),
        .FIR_OUT_SHIFT    (AUDIO_FIR_OUT_SHIFT)
    ) u_audec (
        .clk(clk), .rst_n(rst_n),
        // AXI-Lite slave port AUDEC_IDX (FIR coefs at low addresses)
        .s_axil_awaddr (m_axil_awaddr [AUDEC_IDX*32 +: 32]),
        .s_axil_awprot (m_axil_awprot [AUDEC_IDX*3  +: 3]),
        .s_axil_awvalid(m_axil_awvalid[AUDEC_IDX]),
        .s_axil_awready(m_axil_awready[AUDEC_IDX]),
        .s_axil_wdata  (m_axil_wdata  [AUDEC_IDX*32 +: 32]),
        .s_axil_wstrb  (m_axil_wstrb  [AUDEC_IDX*4  +: 4]),
        .s_axil_wvalid (m_axil_wvalid [AUDEC_IDX]),
        .s_axil_wready (m_axil_wready [AUDEC_IDX]),
        .s_axil_bresp  (m_axil_bresp  [AUDEC_IDX*2  +: 2]),
        .s_axil_bvalid (m_axil_bvalid [AUDEC_IDX]),
        .s_axil_bready (m_axil_bready [AUDEC_IDX]),
        .s_axil_araddr (m_axil_araddr [AUDEC_IDX*32 +: 32]),
        .s_axil_arprot (m_axil_arprot [AUDEC_IDX*3  +: 3]),
        .s_axil_arvalid(m_axil_arvalid[AUDEC_IDX]),
        .s_axil_arready(m_axil_arready[AUDEC_IDX]),
        .s_axil_rdata  (m_axil_rdata  [AUDEC_IDX*32 +: 32]),
        .s_axil_rresp  (m_axil_rresp  [AUDEC_IDX*2  +: 2]),
        .s_axil_rvalid (m_axil_rvalid [AUDEC_IDX]),
        .s_axil_rready (m_axil_rready [AUDEC_IDX]),
        // AXIS
        .s_axis_tdata  (de2ad_tdata),
        .s_axis_tvalid (de2ad_tvalid),
        .s_axis_tready (de2ad_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready)
    );

endmodule

`default_nettype wire
