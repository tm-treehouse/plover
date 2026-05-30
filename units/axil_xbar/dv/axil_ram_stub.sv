// =============================================================================
// axil_ram_stub.sv
//
// Minimal behavioural AXI-Lite slave: a small RAM that responds to reads
// and writes with RESP_OKAY. Used as a stand-in target in unit tests for
// fabric blocks (axil_xbar) so the DV doesn't need to pull in real
// peripherals.
//
// Not synthesizable; testbench-only. AXI handshakes are intentionally
// boring (no backpressure inserted on the AXI ready signals beyond what's
// required to be legal) so the test sees clean, predictable routing
// rather than coverage of stall scenarios — those belong in the unit DVs
// of real peripherals.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module axil_ram_stub #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned DATA_WIDTH = 32,
    parameter int unsigned MEM_BYTES  = 256
) (
    input  wire                         clk,
    input  wire                         rst_n,

    input  wire [ADDR_WIDTH-1:0]        s_axil_awaddr,
    input  wire [2:0]                   s_axil_awprot,
    input  wire                         s_axil_awvalid,
    output reg                          s_axil_awready,

    input  wire [DATA_WIDTH-1:0]        s_axil_wdata,
    input  wire [(DATA_WIDTH/8)-1:0]    s_axil_wstrb,
    input  wire                         s_axil_wvalid,
    output reg                          s_axil_wready,

    output reg  [1:0]                   s_axil_bresp,
    output reg                          s_axil_bvalid,
    input  wire                         s_axil_bready,

    input  wire [ADDR_WIDTH-1:0]        s_axil_araddr,
    input  wire [2:0]                   s_axil_arprot,
    input  wire                         s_axil_arvalid,
    output reg                          s_axil_arready,

    output reg  [DATA_WIDTH-1:0]        s_axil_rdata,
    output reg  [1:0]                   s_axil_rresp,
    output reg                          s_axil_rvalid,
    input  wire                         s_axil_rready
);
    localparam int unsigned WORDS = MEM_BYTES / (DATA_WIDTH/8);
    localparam int unsigned IDX_W = $clog2(WORDS);

    reg [DATA_WIDTH-1:0] mem [WORDS];

    reg [ADDR_WIDTH-1:0] aw_q;
    reg                  aw_have, w_have;
    reg [DATA_WIDTH-1:0] w_data_q;
    reg [(DATA_WIDTH/8)-1:0] w_strb_q;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            s_axil_awready <= 1'b0;
            s_axil_wready  <= 1'b0;
            s_axil_bvalid  <= 1'b0;
            s_axil_bresp   <= 2'b00;
            s_axil_arready <= 1'b0;
            s_axil_rvalid  <= 1'b0;
            s_axil_rdata   <= '0;
            s_axil_rresp   <= 2'b00;
            aw_q     <= '0;
            aw_have  <= 1'b0;
            w_have   <= 1'b0;
            w_data_q <= '0;
            w_strb_q <= '0;
            for (int i = 0; i < WORDS; i++) mem[i] <= '0;
        end else begin
            // Write address handshake.
            if (s_axil_awvalid && !aw_have) begin
                s_axil_awready <= 1'b1;
                aw_q           <= s_axil_awaddr;
                aw_have        <= 1'b1;
            end else begin
                s_axil_awready <= 1'b0;
            end

            // Write data handshake.
            if (s_axil_wvalid && !w_have) begin
                s_axil_wready <= 1'b1;
                w_data_q      <= s_axil_wdata;
                w_strb_q      <= s_axil_wstrb;
                w_have        <= 1'b1;
            end else begin
                s_axil_wready <= 1'b0;
            end

            // When both halves are present, do the write and issue B.
            if (aw_have && w_have && !s_axil_bvalid) begin
                automatic int idx = aw_q[2 +: IDX_W];
                for (int b = 0; b < DATA_WIDTH/8; b++) begin
                    if (w_strb_q[b])
                        mem[idx][b*8 +: 8] <= w_data_q[b*8 +: 8];
                end
                s_axil_bvalid <= 1'b1;
                s_axil_bresp  <= 2'b00;
                aw_have <= 1'b0;
                w_have  <= 1'b0;
            end else if (s_axil_bvalid && s_axil_bready) begin
                s_axil_bvalid <= 1'b0;
            end

            // Read.
            if (s_axil_arvalid && !s_axil_arready && !s_axil_rvalid) begin
                automatic int idx = s_axil_araddr[2 +: IDX_W];
                s_axil_arready <= 1'b1;
                s_axil_rvalid  <= 1'b1;
                s_axil_rdata   <= mem[idx];
                s_axil_rresp   <= 2'b00;
            end else begin
                s_axil_arready <= 1'b0;
                if (s_axil_rvalid && s_axil_rready)
                    s_axil_rvalid <= 1'b0;
            end
        end
    end

endmodule

`default_nettype wire
