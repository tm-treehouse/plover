// =============================================================================
// plover.sv — project top
//
// Top-level integration of the verified sub-units under units/. Exposes
// one AXI4-Lite slave port (s_axil_*) and one AXI4-Stream slave port
// (s_axis_*) plus debug outputs.
//
// An axil_xbar inside the top fans the single AXI-Lite slave out to the
// per-unit slaves:
//
//   0x0000_0000 .. 0x0000_0FFF  ->  axil_shell  (4 KB page)
//   0x0000_1000 .. 0x0000_1FFF  ->  syscon      (4 KB page)
//
// Addresses outside those ranges return AXI DECERR.
//
// syscon.soft_rst_n gates the counter's reset: a software write of 1 to
// SOFT_RST.CORE (offset 0x08 on the syscon page, i.e. 0x0000_1008
// host-visible) pulses soft_rst_n low for syscon's SOFT_RST_CYCLES (default
// 8) cycles, which holds the counter in reset for that window. The AXI
// endpoints stay alive because they're only reset by the global rst_n.
//
// Known limitation, still: axil_shell does not yet expose its CONTROL bits
// as ports, so the counter's enable/clear are still tied to constants here.
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
    parameter int unsigned   XBAR_OUTPUT_REG_STAGES = 0
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

    // ---- External AXI4-Stream input to stream_sink ----------------------
    input  wire [31:0]                s_axis_tdata,
    input  wire                       s_axis_tvalid,
    output wire                       s_axis_tready,
    input  wire                       s_axis_tlast,

    // ---- Observable outputs (debug / status) ----------------------------
    output wire [COUNTER_WIDTH-1:0]   count,
    output wire [31:0]                sink_beat_count,
    output wire [31:0]                sink_data_xor
);

    // -----------------------------------------------------------------
    // Constants and small signals.
    // -----------------------------------------------------------------
    localparam int unsigned N_SLAVES = 2;
    localparam int unsigned SHELL_IDX  = 0;
    localparam int unsigned SYSCON_IDX = 1;

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
        .SLAVE_BASE        ('{32'h0000_0000, 32'h0000_1000}),
        .SLAVE_MASK        ('{32'hFFFF_F000, 32'hFFFF_F000})
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

    stream_sink #(
        .WIDTH(32)
    ) u_stream_sink (
        .clk           (clk),
        .rst_n         (rst_n),
        .s_axis_tdata  (s_axis_tdata),
        .s_axis_tvalid (s_axis_tvalid),
        .s_axis_tready (s_axis_tready),
        .s_axis_tlast  (s_axis_tlast),
        .beat_count    (sink_beat_count),
        .data_xor      (sink_data_xor)
    );

endmodule

`default_nettype wire
