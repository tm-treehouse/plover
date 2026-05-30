// =============================================================================
// stream_sink.sv
//
// AXI4-Stream slave that absorbs every beat (always TREADY=1, no
// backpressure). For each accepted beat it:
//   * increments beat_count
//   * XORs TDATA into data_xor
//
// These two outputs are intended for observation by a testbench (or by
// a debug peripheral in a real system). The block is intentionally
// stateless beyond those two counters; pulse-clear semantics aren't
// needed for the verification-stub use case it's serving.
//
// TLAST is accepted but doesn't gate counting — the sink doesn't
// distinguish packet boundaries. Real downstream consumers would.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module stream_sink #(
    parameter int unsigned WIDTH = 32
) (
    input  wire                 clk,
    input  wire                 rst_n,

    // ---- AXI4-Stream slave ----
    input  wire [WIDTH-1:0]     s_axis_tdata,
    input  wire                 s_axis_tvalid,
    output wire                 s_axis_tready,
    input  wire                 s_axis_tlast,   // accepted but unused

    // ---- Debug outputs ----
    output reg  [31:0]          beat_count,
    output reg  [WIDTH-1:0]     data_xor
);

    // Always ready: no backpressure in the stub. A real sink with a FIFO
    // would lower this when the FIFO is full.
    assign s_axis_tready = 1'b1;

    wire beat = s_axis_tvalid & s_axis_tready;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            beat_count <= '0;
            data_xor   <= '0;
        end else if (beat) begin
            beat_count <= beat_count + 32'd1;
            data_xor   <= data_xor ^ s_axis_tdata;
        end
    end

    // Suppress unused-signal warning on TLAST.
    wire _unused_tlast = s_axis_tlast;

endmodule

`default_nettype wire
