// =============================================================================
// counter.sv
//
// A small synchronous up-counter used as the template sub-unit for the unit-
// testing scaffolding under dv/counter/. The block is deliberately tiny so the
// scaffolding shape is visible without DUT behaviour stealing focus:
//
//   * counts up by one on each rising edge of clk when enable is high
//   * holds value when enable is low
//   * synchronous clear forces the count to zero (regardless of enable)
//   * wraps at 2**WIDTH
//   * active-low reset, OpenTitan-style
//
// To repurpose this as a real sub-unit DV, replace counter.sv with your block,
// update WIDTH / port list in cfg + driver + monitor, and adjust the reference
// model in counter_env.RefModel.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module counter #(
    parameter int unsigned WIDTH = 8
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             clear,
    input  wire             enable,
    output reg  [WIDTH-1:0] count
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)        count <= '0;
        else if (clear)    count <= '0;
        else if (enable)   count <= count + 1'b1;
    end

endmodule

`default_nettype wire
