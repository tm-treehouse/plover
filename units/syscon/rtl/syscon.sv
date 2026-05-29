// =============================================================================
// syscon.sv
//
// System controller block. AXI4-Lite slave hosting:
//
//   0x00  VERSION       semver + dirty bit (build-time, via header)
//   0x04  VERSION_HASH  short git hash (build-time, via header)
//   0x08  SOFT_RST      write-1-to-pulse bit0 -> soft_rst_n low (CYCLES cycles)
//   0x0C  RESET_CAUSE   latched POR / SOFT bits; W1C
//   0x10  FEATURES      build-time constants
//
// Version comes from units/syscon/rdl/gen_version.py at build time (writes
// syscon_version_pkg.svh in the build directory). For sim/test, set the
// VERSION_OVERRIDE parameter to non-zero and that value is used instead.
//
// The soft-reset output (soft_rst_n) is active-low, synchronous to clk, and
// asserts for SOFT_RST_CYCLES cycles after a successful write of 1 to
// SOFT_RST.CORE. While asserted, RESET_CAUSE.SOFT latches.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

`include "syscon_version_pkg.svh"

module syscon #(
    // For sim/test only: if non-zero, used as the {DIRTY,PATCH,MINOR,MAJOR}
    // value of the VERSION register (bits 24:0 — bit24 is dirty). When zero
    // (default), the value comes from the build-time-generated header.
    parameter logic [31:0] VERSION_OVERRIDE = 32'h0,
    // Likewise for the hash register.
    parameter logic [31:0] VERSION_HASH_OVERRIDE = 32'h0,
    // Soft-reset pulse width in clk cycles.
    parameter int unsigned SOFT_RST_CYCLES = 8
) (
    input  wire          clk,
    input  wire          rst_n,

    // ---- AXI4-Lite slave ----
    input  wire [7:0]    s_axil_awaddr,
    input  wire [2:0]    s_axil_awprot,
    input  wire          s_axil_awvalid,
    output reg           s_axil_awready,

    input  wire [31:0]   s_axil_wdata,
    input  wire [3:0]    s_axil_wstrb,
    input  wire          s_axil_wvalid,
    output reg           s_axil_wready,

    output reg  [1:0]    s_axil_bresp,
    output reg           s_axil_bvalid,
    input  wire          s_axil_bready,

    input  wire [7:0]    s_axil_araddr,
    input  wire [2:0]    s_axil_arprot,
    input  wire          s_axil_arvalid,
    output reg           s_axil_arready,

    output reg  [31:0]   s_axil_rdata,
    output reg  [1:0]    s_axil_rresp,
    output reg           s_axil_rvalid,
    input  wire          s_axil_rready,

    // ---- Soft-reset output to the core (active-low, synchronous) ----
    output reg           soft_rst_n
);

    localparam [1:0] RESP_OKAY = 2'b00;

    // ---- Resolved version values (parameter override wins if non-zero) ---
    wire [31:0] version_value =
        (VERSION_OVERRIDE != 32'h0) ? VERSION_OVERRIDE
                                    : `SYSCON_VERSION_VALUE;
    wire [31:0] version_hash =
        (VERSION_HASH_OVERRIDE != 32'h0) ? VERSION_HASH_OVERRIDE
                                         : `SYSCON_VERSION_HASH;

    localparam [31:0] FEATURES_VALUE = 32'h0000_0001;  // HAS_COUNTER=1

    // ---- Register offsets (word-addressed) ------------------------------
    localparam [5:0] OFF_VERSION      = 6'h00;
    localparam [5:0] OFF_VERSION_HASH = 6'h01;
    localparam [5:0] OFF_SOFT_RST     = 6'h02;
    localparam [5:0] OFF_RESET_CAUSE  = 6'h03;
    localparam [5:0] OFF_FEATURES     = 6'h04;

    // ---- Soft-reset pulse generator -------------------------------------
    reg [$clog2(SOFT_RST_CYCLES+1)-1:0] rst_cnt;
    wire soft_rst_req;        // pulsed by write to SOFT_RST.CORE

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rst_cnt    <= '0;
            soft_rst_n <= 1'b1;
        end else if (soft_rst_req) begin
            rst_cnt    <= SOFT_RST_CYCLES[$bits(rst_cnt)-1:0];
            soft_rst_n <= 1'b0;
        end else if (rst_cnt != 0) begin
            rst_cnt    <= rst_cnt - 1'b1;
            soft_rst_n <= 1'b0;
        end else begin
            soft_rst_n <= 1'b1;
        end
    end

    // ---- RESET_CAUSE latch ----------------------------------------------
    // POR bit set on hardware reset deassertion edge — simplest model: set
    // on the first clock after !rst_n. SOFT set when soft_rst_req fires.
    // Both are software-write-1-to-clear.
    reg cause_por_set, cause_por;
    reg cause_soft;
    reg w1c_por_clear, w1c_soft_clear;
    reg cause_seed_done;   // ensures POR is set exactly once per reset

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cause_por        <= 1'b0;
            cause_soft       <= 1'b0;
            cause_seed_done  <= 1'b0;
        end else begin
            if (!cause_seed_done) begin
                cause_por       <= 1'b1;
                cause_seed_done <= 1'b1;
            end else if (w1c_por_clear) begin
                cause_por <= 1'b0;
            end

            if (soft_rst_req)
                cause_soft <= 1'b1;
            else if (w1c_soft_clear)
                cause_soft <= 1'b0;
        end
    end

    // ---- AXI-Lite write path --------------------------------------------
    reg                aw_hs, w_hs;
    reg [7:0]          awaddr_q;
    reg [31:0]         wdata_q;
    reg [3:0]          wstrb_q;
    wire write_commit = aw_hs && w_hs && !s_axil_bvalid;

    // Self-pulsing write detect for SOFT_RST.CORE
    assign soft_rst_req = write_commit &&
                          (awaddr_q[7:2] == OFF_SOFT_RST) &&
                          wstrb_q[0] && wdata_q[0];

    always @(*) begin
        w1c_por_clear  = 1'b0;
        w1c_soft_clear = 1'b0;
        if (write_commit && (awaddr_q[7:2] == OFF_RESET_CAUSE) && wstrb_q[0]) begin
            w1c_por_clear  = wdata_q[0];
            w1c_soft_clear = wdata_q[1];
        end
    end

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
                // All write effects are captured by combinational logic above
                // (soft_rst_req, w1c_*). The register file proper is read-only
                // or self-clearing; nothing else needs storage here.
                s_axil_bvalid <= 1'b1;
                s_axil_bresp  <= RESP_OKAY;
                aw_hs <= 1'b0;
                w_hs  <= 1'b0;
            end else if (s_axil_bvalid && s_axil_bready) begin
                s_axil_bvalid <= 1'b0;
            end
        end
    end

    // ---- AXI-Lite read path ---------------------------------------------
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
                case (s_axil_araddr[7:2])
                    OFF_VERSION:      s_axil_rdata <= version_value;
                    OFF_VERSION_HASH: s_axil_rdata <= version_hash;
                    OFF_SOFT_RST:     s_axil_rdata <= 32'h0;     // pulse reg
                    OFF_RESET_CAUSE:  s_axil_rdata <= {30'h0, cause_soft, cause_por};
                    OFF_FEATURES:     s_axil_rdata <= FEATURES_VALUE;
                    default:          s_axil_rdata <= 32'h0;
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
