// =============================================================================
// plover.sv — project top
//
// Top-level integration of the verified sub-units under units/.
//
// Two AXI4-Lite slave ports are exposed at the top:
//   * s_axil_*   -> axil_shell (general register endpoint at the boundary)
//   * s_syscon_* -> syscon     (system controller: version, reset, features)
//
// The host (or external decoder) arbitrates which slave to target. Combining
// these into a single host port would require an AXI-Lite crossbar/decoder,
// which would itself be a verified sub-unit — that's a fine future step, but
// keeping them split at this stage keeps the integration small and honest.
//
// syscon.soft_rst_n gates the counter's reset: a software write of 1 to
// SOFT_RST.CORE (offset 0x08 on the syscon slave) pulses soft_rst_n low for
// syscon's SOFT_RST_CYCLES (default 8) cycles, which holds the counter in
// reset for that window. The AXI-Lite endpoints stay alive because they're
// only reset by the global rst_n.
//
// Known limitation, still: axil_shell does not yet expose its CONTROL bits
// as ports, so the counter's enable/clear are still tied to constants here.
// The natural follow-up is to widen the shell's port list and source these
// from CONTROL.ENABLE / a CONTROL spare bit, at which point this top
// integration meaningfully programs counter behaviour through the shell.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module plover #(
    parameter int unsigned   COUNTER_WIDTH         = 8,
    // Pass-through to u_syscon for deterministic version values in sim/test.
    // Defaults of 0 cause syscon to use the build-time-generated header.
    parameter logic [31:0]   VERSION_OVERRIDE      = 32'h0,
    parameter logic [31:0]   VERSION_HASH_OVERRIDE = 32'h0
) (
    input  wire                       clk,
    input  wire                       rst_n,

    // ---- External AXI4-Lite slave to axil_shell -------------------------
    input  wire [7:0]                 s_axil_awaddr,
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

    input  wire [7:0]                 s_axil_araddr,
    input  wire [2:0]                 s_axil_arprot,
    input  wire                       s_axil_arvalid,
    output wire                       s_axil_arready,

    output wire [31:0]                s_axil_rdata,
    output wire [1:0]                 s_axil_rresp,
    output wire                       s_axil_rvalid,
    input  wire                       s_axil_rready,

    // ---- External AXI4-Lite slave to syscon -----------------------------
    input  wire [7:0]                 s_syscon_awaddr,
    input  wire [2:0]                 s_syscon_awprot,
    input  wire                       s_syscon_awvalid,
    output wire                       s_syscon_awready,

    input  wire [31:0]                s_syscon_wdata,
    input  wire [3:0]                 s_syscon_wstrb,
    input  wire                       s_syscon_wvalid,
    output wire                       s_syscon_wready,

    output wire [1:0]                 s_syscon_bresp,
    output wire                       s_syscon_bvalid,
    input  wire                       s_syscon_bready,

    input  wire [7:0]                 s_syscon_araddr,
    input  wire [2:0]                 s_syscon_arprot,
    input  wire                       s_syscon_arvalid,
    output wire                       s_syscon_arready,

    output wire [31:0]                s_syscon_rdata,
    output wire [1:0]                 s_syscon_rresp,
    output wire                       s_syscon_rvalid,
    input  wire                       s_syscon_rready,

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

    // axil_shell does not currently surface CONTROL on its port list, so the
    // counter inputs stay structural placeholders. Once the shell exposes
    // CONTROL.ENABLE / a CONTROL spare bit, replace these with the real wires.
    wire counter_enable = 1'b1;
    wire counter_clear  = 1'b0;

    // Soft-reset from syscon, gated with the global reset to form the
    // counter's reset. Active-low both sides; AND gives "reset asserted if
    // either global rst_n is low or syscon's soft_rst_n is low."
    wire syscon_soft_rst_n;
    wire counter_rst_n = rst_n & syscon_soft_rst_n;

    axil_shell #(
        .ADDR_WIDTH(8),
        .DATA_WIDTH(32)
    ) u_axil_shell (
        .clk            (clk),
        .rst_n          (rst_n),
        .s_axil_awaddr  (s_axil_awaddr),
        .s_axil_awprot  (s_axil_awprot),
        .s_axil_awvalid (s_axil_awvalid),
        .s_axil_awready (s_axil_awready),
        .s_axil_wdata   (s_axil_wdata),
        .s_axil_wstrb   (s_axil_wstrb),
        .s_axil_wvalid  (s_axil_wvalid),
        .s_axil_wready  (s_axil_wready),
        .s_axil_bresp   (s_axil_bresp),
        .s_axil_bvalid  (s_axil_bvalid),
        .s_axil_bready  (s_axil_bready),
        .s_axil_araddr  (s_axil_araddr),
        .s_axil_arprot  (s_axil_arprot),
        .s_axil_arvalid (s_axil_arvalid),
        .s_axil_arready (s_axil_arready),
        .s_axil_rdata   (s_axil_rdata),
        .s_axil_rresp   (s_axil_rresp),
        .s_axil_rvalid  (s_axil_rvalid),
        .s_axil_rready  (s_axil_rready)
    );

    syscon #(
        .VERSION_OVERRIDE      (VERSION_OVERRIDE),
        .VERSION_HASH_OVERRIDE (VERSION_HASH_OVERRIDE)
    ) u_syscon (
        .clk            (clk),
        .rst_n          (rst_n),
        .s_axil_awaddr  (s_syscon_awaddr),
        .s_axil_awprot  (s_syscon_awprot),
        .s_axil_awvalid (s_syscon_awvalid),
        .s_axil_awready (s_syscon_awready),
        .s_axil_wdata   (s_syscon_wdata),
        .s_axil_wstrb   (s_syscon_wstrb),
        .s_axil_wvalid  (s_syscon_wvalid),
        .s_axil_wready  (s_syscon_wready),
        .s_axil_bresp   (s_syscon_bresp),
        .s_axil_bvalid  (s_syscon_bvalid),
        .s_axil_bready  (s_syscon_bready),
        .s_axil_araddr  (s_syscon_araddr),
        .s_axil_arprot  (s_syscon_arprot),
        .s_axil_arvalid (s_syscon_arvalid),
        .s_axil_arready (s_syscon_arready),
        .s_axil_rdata   (s_syscon_rdata),
        .s_axil_rresp   (s_syscon_rresp),
        .s_axil_rvalid  (s_syscon_rvalid),
        .s_axil_rready  (s_syscon_rready),
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
