// =============================================================================
// plover.sv — project top
//
// Top-level integration of the verified sub-units under units/. Exposes
// one AXI4-Lite slave port (s_axil_*) and an AXI4-Stream signal data path
// (s_axis_*  in, m_axis_*  out) with a CIC-decimator -> FIR filter chain
// between them. Software-programmable FIR coefficients land at the FIR's
// AXI-Lite slave port via the xbar.
//
// Address map (axil_xbar):
//   0x0000_0000 .. 0x0000_0FFF  ->  axil_shell    (4 KB page)
//   0x0000_1000 .. 0x0000_1FFF  ->  syscon        (4 KB page)
//   0x0000_2000 .. 0x0000_2FFF  ->  fir_filter    (4 KB page — coef bank)
// Addresses outside those ranges return AXI DECERR.
//
// DSP signal chain:
//   s_axis_*  (16-bit signed, IN_RATE) -> cic_decimator(R=4, N=3)
//                                      -> fir_filter(N_TAPS=8)
//                                      -> m_axis_*  (16-bit signed, OUT_RATE = IN_RATE/4)
// FIR coefficients are hot-updatable from software via the xbar.
//
// syscon.soft_rst_n gates the counter's reset; the chain shares the main
// rst_n (the AXIS BFMs in DV expect that boundary). On reset the FIR's
// coefficients clear to zero — software must reprogram before useful
// output. The integration tests model this in lock-step.
//
// CONTROL.ENABLE from axil_shell still gates the legacy free-running
// counter (kept around as a debug heartbeat). It does not gate the DSP
// chain — the chain runs whenever samples arrive and coefficients are
// programmed.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module plover #(
    parameter int unsigned   COUNTER_WIDTH         = 8,
    // Pass-through to u_syscon for deterministic version values in sim/test.
    // Defaults of 0 cause syscon to use the build-time-generated header.
    parameter logic [31:0]   VERSION_OVERRIDE      = 32'h0,
    parameter logic [31:0]   VERSION_HASH_OVERRIDE = 32'h0,
    // Crossbar register-stage knobs. Default 0 = combinational.
    parameter int unsigned   XBAR_INPUT_REG_STAGES  = 0,
    parameter int unsigned   XBAR_OUTPUT_REG_STAGES = 0,
    // ---- DSP chain parameters ----
    // CIC decimator: N stages, decimation R, differential delay M.
    parameter int unsigned   CIC_STAGES = 3,
    parameter int unsigned   CIC_DECIM  = 4,
    parameter int unsigned   CIC_DELAY  = 1,
    // Sample width and Q-format used end-to-end through the chain.
    // The chain (CIC -> FIR) carries SAMPLE_W-bit signed samples whose
    // value is interpreted with SAMPLE_INT_W integer bits (including
    // sign) and SAMPLE_FRAC_W fractional bits. Default Q1.15: samples
    // in [-1.0, +1.0). The Q params are informational — the arithmetic
    // operates on integers and is unity-DC-gain through the CIC, so
    // input and output Q-positions are the same.
    parameter int unsigned   SAMPLE_W        = 16,
    parameter int unsigned   SAMPLE_INT_W    = 1,
    parameter int unsigned   SAMPLE_FRAC_W   = SAMPLE_W - SAMPLE_INT_W,
    // FIR: N taps, coefficient width + Q-format, OUT_SHIFT for output
    // Q-alignment. With the default FIR_COEF_INT_W=1 / FIR_COEF_FRAC_W=15,
    // OUT_SHIFT=15 = FIR_COEF_FRAC_W gives back samples at the input
    // Q-position. If coefficients were e.g. Q3.13, set FIR_COEF_INT_W=3,
    // FIR_COEF_FRAC_W=13, FIR_OUT_SHIFT=13 to preserve the input
    // Q-position through the multiply.
    parameter int unsigned   FIR_N_TAPS      = 8,
    parameter int unsigned   FIR_COEF_W      = 16,
    parameter int unsigned   FIR_COEF_INT_W  = 1,
    parameter int unsigned   FIR_COEF_FRAC_W = FIR_COEF_W - FIR_COEF_INT_W,
    parameter int            FIR_OUT_SHIFT   = FIR_COEF_FRAC_W
) (
    input  wire                       clk,
    input  wire                       rst_n,

    // ---- Single external AXI4-Lite slave (fans out via xbar) -----------
    input  wire [31:0]                s_axil_awaddr,
    input  wire [2:0]                 s_axil_awprot,
    input  wire                       s_axil_awvalid,
    output wire                       s_axil_awready,

    input  wire [31:0]                s_axil_wdata,
    input  wire [3:0]                 s_axil_wstrb,
    input  wire                       s_axil_wvalid,
    output wire                       s_axil_wready,

    output wire [1:0]                 s_axil_bresp,
    output wire                       s_axil_bvalid,
    input  wire                       s_axil_bready,

    input  wire [31:0]                s_axil_araddr,
    input  wire [2:0]                 s_axil_arprot,
    input  wire                       s_axil_arvalid,
    output wire                       s_axil_arready,

    output wire [31:0]                s_axil_rdata,
    output wire [1:0]                 s_axil_rresp,
    output wire                       s_axil_rvalid,
    input  wire                       s_axil_rready,

    // ---- DSP chain AXI4-Stream input (slow / pre-decimation samples) ---
    // Signed SAMPLE_W-bit samples driven into the CIC decimator.
    input  wire signed [SAMPLE_W-1:0] s_axis_tdata,
    input  wire                       s_axis_tvalid,
    output wire                       s_axis_tready,

    // ---- DSP chain AXI4-Stream output (filtered / decimated samples) ---
    // Signed SAMPLE_W-bit samples emitted from the FIR.
    output wire signed [SAMPLE_W-1:0] m_axis_tdata,
    output wire                       m_axis_tvalid,
    input  wire                       m_axis_tready,

    // ---- Observable outputs (debug / status) ----------------------------
    output wire [COUNTER_WIDTH-1:0]   count
);

    // -----------------------------------------------------------------
    // Constants and small signals.
    // -----------------------------------------------------------------
    localparam int unsigned N_SLAVES = 3;
    localparam int unsigned SHELL_IDX  = 0;
    localparam int unsigned SYSCON_IDX = 1;
    localparam int unsigned FIR_IDX    = 2;

    // Xbar slave page map. Defined as localparams here so the array
    // size is fixed before u_xbar's parameter binding (works around a
    // simulator quirk: named array-typed parameters were being
    // elaboration-checked against the module's default N_SLAVES rather
    // than the override).
    localparam logic [31:0] XBAR_SLAVE_BASE [N_SLAVES] = '{
        32'h0000_0000,  // SHELL_IDX
        32'h0000_1000,  // SYSCON_IDX
        32'h0000_2000   // FIR_IDX
    };
    localparam logic [31:0] XBAR_SLAVE_MASK [N_SLAVES] = '{
        32'hFFFF_F000,
        32'hFFFF_F000,
        32'hFFFF_F000
    };

    // CONTROL fields driven by axil_shell.
    //   * shell_control_enable drives the counter's enable. Software
    //     writes to axil_shell.CONTROL.ENABLE turn the counter on or
    //     off; bug-injection covered in the top integration smoke test.
    //   * shell_control_spare carries the remaining 31 R/W bits of the
    //     CONTROL register. Unused today (bit 0 of SPARE could drive
    //     the counter's `clear`, for instance — left tied off until a
    //     downstream consumer needs it).
    wire        shell_control_enable;
    wire [30:0] shell_control_spare;
    wire counter_enable = shell_control_enable;
    wire counter_clear  = 1'b0;

    wire syscon_soft_rst_n;
    wire counter_rst_n = rst_n & syscon_soft_rst_n;

    // -----------------------------------------------------------------
    // axil_xbar fan-out signals (one bit / one slice per slave).
    // -----------------------------------------------------------------
    wire [N_SLAVES*32-1:0] m_axil_awaddr;
    wire [N_SLAVES*3-1:0]  m_axil_awprot;
    wire [N_SLAVES-1:0]    m_axil_awvalid;
    wire [N_SLAVES-1:0]    m_axil_awready;
    wire [N_SLAVES*32-1:0] m_axil_wdata;
    wire [N_SLAVES*4-1:0]  m_axil_wstrb;
    wire [N_SLAVES-1:0]    m_axil_wvalid;
    wire [N_SLAVES-1:0]    m_axil_wready;
    wire [N_SLAVES*2-1:0]  m_axil_bresp;
    wire [N_SLAVES-1:0]    m_axil_bvalid;
    wire [N_SLAVES-1:0]    m_axil_bready;
    wire [N_SLAVES*32-1:0] m_axil_araddr;
    wire [N_SLAVES*3-1:0]  m_axil_arprot;
    wire [N_SLAVES-1:0]    m_axil_arvalid;
    wire [N_SLAVES-1:0]    m_axil_arready;
    wire [N_SLAVES*32-1:0] m_axil_rdata;
    wire [N_SLAVES*2-1:0]  m_axil_rresp;
    wire [N_SLAVES-1:0]    m_axil_rvalid;
    wire [N_SLAVES-1:0]    m_axil_rready;

    axil_xbar #(
        .N_SLAVES          (N_SLAVES),
        .ADDR_WIDTH        (32),
        .DATA_WIDTH        (32),
        .INPUT_REG_STAGES  (XBAR_INPUT_REG_STAGES),
        .OUTPUT_REG_STAGES (XBAR_OUTPUT_REG_STAGES),
        .SLAVE_BASE        (XBAR_SLAVE_BASE),
        .SLAVE_MASK        (XBAR_SLAVE_MASK)
    ) u_xbar (
        .clk(clk), .rst_n(rst_n),

        // External slave port
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

        // Fan-out to per-slave master ports
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
    // axil_shell on slave-0 port. Peripheral uses an 8-bit AWADDR so we
    // take the low 8 bits of the 32-bit AWADDR coming from the xbar.
    // -----------------------------------------------------------------
    axil_shell #(
        .ADDR_WIDTH(8),
        .DATA_WIDTH(32)
    ) u_axil_shell (
        .clk            (clk),
        .rst_n          (rst_n),
        .s_axil_awaddr  (m_axil_awaddr [SHELL_IDX*32 +: 8]),
        .s_axil_awprot  (m_axil_awprot [SHELL_IDX*3  +: 3]),
        .s_axil_awvalid (m_axil_awvalid[SHELL_IDX]),
        .s_axil_awready (m_axil_awready[SHELL_IDX]),
        .s_axil_wdata   (m_axil_wdata  [SHELL_IDX*32 +: 32]),
        .s_axil_wstrb   (m_axil_wstrb  [SHELL_IDX*4  +: 4]),
        .s_axil_wvalid  (m_axil_wvalid [SHELL_IDX]),
        .s_axil_wready  (m_axil_wready [SHELL_IDX]),
        .s_axil_bresp   (m_axil_bresp  [SHELL_IDX*2  +: 2]),
        .s_axil_bvalid  (m_axil_bvalid [SHELL_IDX]),
        .s_axil_bready  (m_axil_bready [SHELL_IDX]),
        .s_axil_araddr  (m_axil_araddr [SHELL_IDX*32 +: 8]),
        .s_axil_arprot  (m_axil_arprot [SHELL_IDX*3  +: 3]),
        .s_axil_arvalid (m_axil_arvalid[SHELL_IDX]),
        .s_axil_arready (m_axil_arready[SHELL_IDX]),
        .s_axil_rdata   (m_axil_rdata  [SHELL_IDX*32 +: 32]),
        .s_axil_rresp   (m_axil_rresp  [SHELL_IDX*2  +: 2]),
        .s_axil_rvalid  (m_axil_rvalid [SHELL_IDX]),
        .s_axil_rready  (m_axil_rready [SHELL_IDX]),
        .control_enable (shell_control_enable),
        .control_spare  (shell_control_spare)
    );

    syscon #(
        .VERSION_OVERRIDE      (VERSION_OVERRIDE),
        .VERSION_HASH_OVERRIDE (VERSION_HASH_OVERRIDE)
    ) u_syscon (
        .clk            (clk),
        .rst_n          (rst_n),
        .s_axil_awaddr  (m_axil_awaddr [SYSCON_IDX*32 +: 8]),
        .s_axil_awprot  (m_axil_awprot [SYSCON_IDX*3  +: 3]),
        .s_axil_awvalid (m_axil_awvalid[SYSCON_IDX]),
        .s_axil_awready (m_axil_awready[SYSCON_IDX]),
        .s_axil_wdata   (m_axil_wdata  [SYSCON_IDX*32 +: 32]),
        .s_axil_wstrb   (m_axil_wstrb  [SYSCON_IDX*4  +: 4]),
        .s_axil_wvalid  (m_axil_wvalid [SYSCON_IDX]),
        .s_axil_wready  (m_axil_wready [SYSCON_IDX]),
        .s_axil_bresp   (m_axil_bresp  [SYSCON_IDX*2  +: 2]),
        .s_axil_bvalid  (m_axil_bvalid [SYSCON_IDX]),
        .s_axil_bready  (m_axil_bready [SYSCON_IDX]),
        .s_axil_araddr  (m_axil_araddr [SYSCON_IDX*32 +: 8]),
        .s_axil_arprot  (m_axil_arprot [SYSCON_IDX*3  +: 3]),
        .s_axil_arvalid (m_axil_arvalid[SYSCON_IDX]),
        .s_axil_arready (m_axil_arready[SYSCON_IDX]),
        .s_axil_rdata   (m_axil_rdata  [SYSCON_IDX*32 +: 32]),
        .s_axil_rresp   (m_axil_rresp  [SYSCON_IDX*2  +: 2]),
        .s_axil_rvalid  (m_axil_rvalid [SYSCON_IDX]),
        .s_axil_rready  (m_axil_rready [SYSCON_IDX]),
        .soft_rst_n     (syscon_soft_rst_n)
    );

    counter #(
        .WIDTH(COUNTER_WIDTH)
    ) u_counter (
        .clk    (clk),
        .rst_n  (counter_rst_n),
        .clear  (counter_clear),
        .enable (counter_enable),
        .count  (count)
    );

    // -----------------------------------------------------------------
    // DSP signal chain: CIC decimator -> FIR
    //
    // Sample data flow:
    //   s_axis_*  (16-bit signed, input rate)
    //     -> cic_decimator (decim by R, internal pipelining)
    //     -> intermediate AXIS (16-bit signed, input_rate / R)
    //     -> fir_filter   (8-tap, software-programmable coefs)
    //     -> m_axis_*  (16-bit signed, output rate = input rate / R)
    //
    // The intermediate AXIS bus between the two units is internal —
    // testbenches observe the chain's output via m_axis_* and the
    // input via s_axis_*. Coefficients land at fir_filter via
    // m_axil_*[FIR_IDX] (xbar slave 2, page 0x0000_2000).
    // -----------------------------------------------------------------

    // CIC -> FIR intermediate AXIS.
    wire signed [SAMPLE_W-1:0]    cic2fir_tdata;
    wire                          cic2fir_tvalid;
    wire                          cic2fir_tready;

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
    ) u_cic_decim (
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
        // AXI-Lite coefficient bank (xbar slave FIR_IDX)
        .s_axil_awaddr (m_axil_awaddr [FIR_IDX*32 +: 32]),
        .s_axil_awprot (m_axil_awprot [FIR_IDX*3  +: 3]),
        .s_axil_awvalid(m_axil_awvalid[FIR_IDX]),
        .s_axil_awready(m_axil_awready[FIR_IDX]),
        .s_axil_wdata  (m_axil_wdata  [FIR_IDX*32 +: 32]),
        .s_axil_wstrb  (m_axil_wstrb  [FIR_IDX*4  +: 4]),
        .s_axil_wvalid (m_axil_wvalid [FIR_IDX]),
        .s_axil_wready (m_axil_wready [FIR_IDX]),
        .s_axil_bresp  (m_axil_bresp  [FIR_IDX*2  +: 2]),
        .s_axil_bvalid (m_axil_bvalid [FIR_IDX]),
        .s_axil_bready (m_axil_bready [FIR_IDX]),
        .s_axil_araddr (m_axil_araddr [FIR_IDX*32 +: 32]),
        .s_axil_arprot (m_axil_arprot [FIR_IDX*3  +: 3]),
        .s_axil_arvalid(m_axil_arvalid[FIR_IDX]),
        .s_axil_arready(m_axil_arready[FIR_IDX]),
        .s_axil_rdata  (m_axil_rdata  [FIR_IDX*32 +: 32]),
        .s_axil_rresp  (m_axil_rresp  [FIR_IDX*2  +: 2]),
        .s_axil_rvalid (m_axil_rvalid [FIR_IDX]),
        .s_axil_rready (m_axil_rready [FIR_IDX]),
        // Sample AXIS in/out
        .s_axis_tdata  (cic2fir_tdata),
        .s_axis_tvalid (cic2fir_tvalid),
        .s_axis_tready (cic2fir_tready),
        .m_axis_tdata  (m_axis_tdata),
        .m_axis_tvalid (m_axis_tvalid),
        .m_axis_tready (m_axis_tready)
    );

endmodule

`default_nettype wire
