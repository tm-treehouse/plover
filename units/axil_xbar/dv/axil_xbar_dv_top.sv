// =============================================================================
// axil_xbar_dv_top.sv
//
// Testbench wrapper for the axil_xbar unit DV. Wires the xbar to two RAM
// stubs at addresses 0x0000_0000 and 0x0000_1000 (4 KB pages) and exposes
// one master-side AXI-Lite port that cocotb drives.
//
// Not synthesized — DV use only.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module axil_xbar_dv_top #(
    parameter int unsigned INPUT_REG_STAGES  = 0,
    parameter int unsigned OUTPUT_REG_STAGES = 0
) (
    input  wire                       clk,
    input  wire                       rst_n,

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
    input  wire                       s_axil_rready
);

    localparam int unsigned N = 2;

    // Per-slave fan-out signals.
    wire [N*32-1:0] m_awaddr;
    wire [N*3-1:0]  m_awprot;
    wire [N-1:0]    m_awvalid;
    wire [N-1:0]    m_awready;
    wire [N*32-1:0] m_wdata;
    wire [N*4-1:0]  m_wstrb;
    wire [N-1:0]    m_wvalid;
    wire [N-1:0]    m_wready;
    wire [N*2-1:0]  m_bresp;
    wire [N-1:0]    m_bvalid;
    wire [N-1:0]    m_bready;
    wire [N*32-1:0] m_araddr;
    wire [N*3-1:0]  m_arprot;
    wire [N-1:0]    m_arvalid;
    wire [N-1:0]    m_arready;
    wire [N*32-1:0] m_rdata;
    wire [N*2-1:0]  m_rresp;
    wire [N-1:0]    m_rvalid;
    wire [N-1:0]    m_rready;

    axil_xbar #(
        .N_SLAVES          (N),
        .ADDR_WIDTH        (32),
        .DATA_WIDTH        (32),
        .INPUT_REG_STAGES  (INPUT_REG_STAGES),
        .OUTPUT_REG_STAGES (OUTPUT_REG_STAGES),
        .SLAVE_BASE        ('{32'h0000_0000, 32'h0000_1000}),
        .SLAVE_MASK        ('{32'hFFFF_F000, 32'hFFFF_F000})
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

        .m_axil_awaddr (m_awaddr),
        .m_axil_awprot (m_awprot),
        .m_axil_awvalid(m_awvalid),
        .m_axil_awready(m_awready),
        .m_axil_wdata  (m_wdata),
        .m_axil_wstrb  (m_wstrb),
        .m_axil_wvalid (m_wvalid),
        .m_axil_wready (m_wready),
        .m_axil_bresp  (m_bresp),
        .m_axil_bvalid (m_bvalid),
        .m_axil_bready (m_bready),
        .m_axil_araddr (m_araddr),
        .m_axil_arprot (m_arprot),
        .m_axil_arvalid(m_arvalid),
        .m_axil_arready(m_arready),
        .m_axil_rdata  (m_rdata),
        .m_axil_rresp  (m_rresp),
        .m_axil_rvalid (m_rvalid),
        .m_axil_rready (m_rready)
    );

    // Slave 0 RAM at 0x0000_0000.
    axil_ram_stub #(.ADDR_WIDTH(32), .DATA_WIDTH(32), .MEM_BYTES(256))
        u_ram0 (
            .clk(clk), .rst_n(rst_n),
            .s_axil_awaddr (m_awaddr[0*32 +: 32]),
            .s_axil_awprot (m_awprot[0*3  +: 3]),
            .s_axil_awvalid(m_awvalid[0]),
            .s_axil_awready(m_awready[0]),
            .s_axil_wdata  (m_wdata [0*32 +: 32]),
            .s_axil_wstrb  (m_wstrb [0*4  +: 4]),
            .s_axil_wvalid (m_wvalid[0]),
            .s_axil_wready (m_wready[0]),
            .s_axil_bresp  (m_bresp [0*2  +: 2]),
            .s_axil_bvalid (m_bvalid[0]),
            .s_axil_bready (m_bready[0]),
            .s_axil_araddr (m_araddr[0*32 +: 32]),
            .s_axil_arprot (m_arprot[0*3  +: 3]),
            .s_axil_arvalid(m_arvalid[0]),
            .s_axil_arready(m_arready[0]),
            .s_axil_rdata  (m_rdata [0*32 +: 32]),
            .s_axil_rresp  (m_rresp [0*2  +: 2]),
            .s_axil_rvalid (m_rvalid[0]),
            .s_axil_rready (m_rready[0])
        );

    // Slave 1 RAM at 0x0000_1000.
    axil_ram_stub #(.ADDR_WIDTH(32), .DATA_WIDTH(32), .MEM_BYTES(256))
        u_ram1 (
            .clk(clk), .rst_n(rst_n),
            .s_axil_awaddr (m_awaddr[1*32 +: 32]),
            .s_axil_awprot (m_awprot[1*3  +: 3]),
            .s_axil_awvalid(m_awvalid[1]),
            .s_axil_awready(m_awready[1]),
            .s_axil_wdata  (m_wdata [1*32 +: 32]),
            .s_axil_wstrb  (m_wstrb [1*4  +: 4]),
            .s_axil_wvalid (m_wvalid[1]),
            .s_axil_wready (m_wready[1]),
            .s_axil_bresp  (m_bresp [1*2  +: 2]),
            .s_axil_bvalid (m_bvalid[1]),
            .s_axil_bready (m_bready[1]),
            .s_axil_araddr (m_araddr[1*32 +: 32]),
            .s_axil_arprot (m_arprot[1*3  +: 3]),
            .s_axil_arvalid(m_arvalid[1]),
            .s_axil_arready(m_arready[1]),
            .s_axil_rdata  (m_rdata [1*32 +: 32]),
            .s_axil_rresp  (m_rresp [1*2  +: 2]),
            .s_axil_rvalid (m_rvalid[1]),
            .s_axil_rready (m_rready[1])
        );

endmodule

`default_nettype wire
