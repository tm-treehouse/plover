// =============================================================================
// axil_skid_buffer.sv
//
// Single-payload AXI-Lite-style register slice with VALID/READY handshake.
// Used to insert optional register stages on the decoder's channels to
// improve timing. With DEPTH=0 the buffer is bypassed (combinational
// pass-through). With DEPTH=1 it inserts one register stage; deeper stages
// chain instances of the same module.
//
// At DEPTH=1 the slice is a true skid buffer: it has two storage slots
// (the main register plus a "skid" slot) so it can absorb one beat while
// the downstream is asserting !ready, without ever blocking upstream. This
// is the standard pattern; sacrificing one slot of buffering for full
// throughput on a registered interface.
//
// The implementation is payload-agnostic — width is parameterized — so the
// same module handles AW (addr+prot), W (data+strb), AR (addr+prot), and
// the response channels (B: resp, R: data+resp).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module axil_skid_buffer #(
    parameter int unsigned WIDTH = 32,
    parameter int unsigned DEPTH = 0    // 0 = bypass; 1+ = registered
) (
    input  wire              clk,
    input  wire              rst_n,

    input  wire [WIDTH-1:0]  s_data,
    input  wire              s_valid,
    output wire              s_ready,

    output wire [WIDTH-1:0]  m_data,
    output wire              m_valid,
    input  wire              m_ready
);

    generate
        if (DEPTH == 0) begin : g_bypass
            assign m_data  = s_data;
            assign m_valid = s_valid;
            assign s_ready = m_ready;
        end else if (DEPTH == 1) begin : g_skid
            // Two-slot skid buffer: main + skid. Always able to accept an
            // upstream beat unless both slots are full.
            reg [WIDTH-1:0] r_main_data;
            reg             r_main_valid;
            reg [WIDTH-1:0] r_skid_data;
            reg             r_skid_valid;

            // We can accept upstream as long as skid is empty.
            assign s_ready = !r_skid_valid;

            assign m_data  = r_main_data;
            assign m_valid = r_main_valid;

            always @(posedge clk or negedge rst_n) begin
                if (!rst_n) begin
                    r_main_valid <= 1'b0;
                    r_skid_valid <= 1'b0;
                    r_main_data  <= '0;
                    r_skid_data  <= '0;
                end else begin
                    // Downstream consumed — promote skid (if any) into main,
                    // or accept fresh upstream into main if no skid.
                    if (m_valid && m_ready) begin
                        if (r_skid_valid) begin
                            r_main_data  <= r_skid_data;
                            r_main_valid <= 1'b1;
                            r_skid_valid <= 1'b0;
                        end else if (s_valid && s_ready) begin
                            r_main_data  <= s_data;
                            r_main_valid <= 1'b1;
                        end else begin
                            r_main_valid <= 1'b0;
                        end
                    end else if (!r_main_valid && s_valid && s_ready) begin
                        // Fill main when empty.
                        r_main_data  <= s_data;
                        r_main_valid <= 1'b1;
                    end else if (r_main_valid && !m_ready
                                 && s_valid && s_ready) begin
                        // Downstream stalled and we still accept upstream
                        // (s_ready = !r_skid_valid). Park in skid.
                        r_skid_data  <= s_data;
                        r_skid_valid <= 1'b1;
                    end
                end
            end
        end else begin : g_chain
            // DEPTH > 1: chain DEPTH instances of DEPTH=1.
            wire [WIDTH-1:0] chain_data  [DEPTH-1:0];
            wire             chain_valid [DEPTH-1:0];
            wire             chain_ready [DEPTH-1:0];

            // First stage takes s_*; last stage drives m_*. Intermediate
            // stages connect to neighbours via the chain_* arrays.
            for (genvar i = 0; i < DEPTH; i++) begin : g_stage
                wire [WIDTH-1:0] in_data;
                wire             in_valid;
                wire             in_ready;
                wire [WIDTH-1:0] out_data;
                wire             out_valid;
                wire             out_ready;

                if (i == 0) begin : g_in_first
                    assign in_data  = s_data;
                    assign in_valid = s_valid;
                end else begin : g_in_chain
                    assign in_data  = chain_data[i-1];
                    assign in_valid = chain_valid[i-1];
                end

                if (i == DEPTH-1) begin : g_out_last
                    assign out_ready = m_ready;
                end else begin : g_out_chain
                    assign out_ready = chain_ready[i+1];
                end

                axil_skid_buffer #(.WIDTH(WIDTH), .DEPTH(1)) u_stage (
                    .clk     (clk),
                    .rst_n   (rst_n),
                    .s_data  (in_data),
                    .s_valid (in_valid),
                    .s_ready (in_ready),
                    .m_data  (out_data),
                    .m_valid (out_valid),
                    .m_ready (out_ready)
                );

                assign chain_data[i]  = out_data;
                assign chain_valid[i] = out_valid;
                assign chain_ready[i] = in_ready;
            end

            assign s_ready = chain_ready[0];
            assign m_data  = chain_data[DEPTH-1];
            assign m_valid = chain_valid[DEPTH-1];
        end
    endgenerate

endmodule

`default_nettype wire
