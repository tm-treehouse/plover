// -----------------------------------------------------------------------------
// axil_shell.sv
//
// Top-level project shell for an FPGA design, exposed over a single AXI4-Lite
// slave port. The shell is deliberately minimal: it is the *endpoint* a host
// (CPU, DMA, or another fabric master) talks to. Drop your real logic in
// where the register effects are, or fan the bus out to sub-blocks.
//
//   Register map (byte addresses, 32-bit data):
//     0x00  SCRATCH    R/W   General-purpose scratch register
//     0x04  CONTROL    R/W   Control bits (design-defined)
//     0x08  STATUS     RO    Status; bit0 mirrors CONTROL[0]
//     0x0C  ID         RO    Constant ID = 0xC0C07B01
//
// AXI4-Lite: 32-bit data, no bursts, no IDs, single outstanding transaction.
// Active-low reset (rst_n), OpenTitan-style.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps
`default_nettype none

module axil_shell #(
    parameter int unsigned ADDR_WIDTH = 8,
    parameter int unsigned DATA_WIDTH = 32
) (
    input  wire                      clk,
    input  wire                      rst_n,

    // ---- Write address channel ----
    input  wire [ADDR_WIDTH-1:0]     s_axil_awaddr,
    input  wire [2:0]                s_axil_awprot,
    input  wire                      s_axil_awvalid,
    output reg                       s_axil_awready,

    // ---- Write data channel ----
    input  wire [DATA_WIDTH-1:0]     s_axil_wdata,
    input  wire [(DATA_WIDTH/8)-1:0] s_axil_wstrb,
    input  wire                      s_axil_wvalid,
    output reg                       s_axil_wready,

    // ---- Write response channel ----
    output reg  [1:0]                s_axil_bresp,
    output reg                       s_axil_bvalid,
    input  wire                      s_axil_bready,

    // ---- Read address channel ----
    input  wire [ADDR_WIDTH-1:0]     s_axil_araddr,
    input  wire [2:0]                s_axil_arprot,
    input  wire                      s_axil_arvalid,
    output reg                       s_axil_arready,

    // ---- Read data channel ----
    output reg  [DATA_WIDTH-1:0]     s_axil_rdata,
    output reg  [1:0]                s_axil_rresp,
    output reg                       s_axil_rvalid,
    input  wire                      s_axil_rready
);

    localparam [1:0] RESP_OKAY = 2'b00;
    localparam int unsigned BYTE_BITS = $clog2(DATA_WIDTH/8);

    // ---- Register storage ----
    reg [DATA_WIDTH-1:0] reg_scratch;
    reg [DATA_WIDTH-1:0] reg_control;
    localparam [DATA_WIDTH-1:0] ID_VALUE = 32'hC0C0_7B01;

    // ---------------------------------------------------------------------
    // Write path: latch AW and W independently, commit + respond.
    // ---------------------------------------------------------------------
    reg                      aw_hs;
    reg                      w_hs;
    reg [ADDR_WIDTH-1:0]     awaddr_q;
    reg [DATA_WIDTH-1:0]     wdata_q;
    reg [(DATA_WIDTH/8)-1:0] wstrb_q;

    wire write_commit = aw_hs && w_hs && !s_axil_bvalid;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axil_awready <= 1'b0;
            s_axil_wready  <= 1'b0;
            s_axil_bvalid  <= 1'b0;
            s_axil_bresp   <= RESP_OKAY;
            aw_hs          <= 1'b0;
            w_hs           <= 1'b0;
            awaddr_q       <= '0;
            wdata_q        <= '0;
            wstrb_q        <= '0;
            reg_scratch    <= '0;
            reg_control    <= '0;
        end else begin
            if (s_axil_awvalid && !aw_hs) begin
                s_axil_awready <= 1'b1;
                aw_hs          <= 1'b1;
                awaddr_q       <= s_axil_awaddr;
            end else begin
                s_axil_awready <= 1'b0;
            end

            if (s_axil_wvalid && !w_hs) begin
                s_axil_wready <= 1'b1;
                w_hs          <= 1'b1;
                wdata_q       <= s_axil_wdata;
                wstrb_q       <= s_axil_wstrb;
            end else begin
                s_axil_wready <= 1'b0;
            end

            if (write_commit) begin
                case (awaddr_q[ADDR_WIDTH-1:BYTE_BITS])
                    'h0: for (int b = 0; b < DATA_WIDTH/8; b++)
                             if (wstrb_q[b]) reg_scratch[b*8 +: 8] <= wdata_q[b*8 +: 8];
                    'h1: for (int b = 0; b < DATA_WIDTH/8; b++)
                             if (wstrb_q[b]) reg_control[b*8 +: 8] <= wdata_q[b*8 +: 8];
                    default: /* STATUS, ID, unmapped: dropped */ ;
                endcase
                s_axil_bvalid <= 1'b1;
                s_axil_bresp  <= RESP_OKAY;
                aw_hs <= 1'b0;
                w_hs  <= 1'b0;
            end else if (s_axil_bvalid && s_axil_bready) begin
                s_axil_bvalid <= 1'b0;
            end
        end
    end

    // ---------------------------------------------------------------------
    // Read path: single-cycle address accept, drive RDATA.
    // ---------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axil_arready <= 1'b0;
            s_axil_rvalid  <= 1'b0;
            s_axil_rdata   <= '0;
            s_axil_rresp   <= RESP_OKAY;
        end else begin
            if (s_axil_arvalid && !s_axil_arready && !s_axil_rvalid) begin
                s_axil_arready <= 1'b1;
                s_axil_rvalid  <= 1'b1;
                s_axil_rresp   <= RESP_OKAY;
                case (s_axil_araddr[ADDR_WIDTH-1:BYTE_BITS])
                    'h0: s_axil_rdata <= reg_scratch;
                    'h1: s_axil_rdata <= reg_control;
                    'h2: s_axil_rdata <= {{(DATA_WIDTH-1){1'b0}}, reg_control[0]};
                    'h3: s_axil_rdata <= ID_VALUE;
                    default: s_axil_rdata <= '0;
                endcase
            end else begin
                s_axil_arready <= 1'b0;
                if (s_axil_rvalid && s_axil_rready)
                    s_axil_rvalid <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
