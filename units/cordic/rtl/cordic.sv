// =============================================================================
// cordic.sv
//
// CORDIC vectoring engine, 16-stage pipelined. Consumes a complex IQ
// sample per clock and produces a (magnitude, phase) pair 16 cycles
// later. Each output handshake corresponds to one earlier input
// handshake (FIFO ordering, no reordering).
//
// Algorithm (per stage k = 0..15):
//
//     sigma_k = -sign(y_k)                      // vectoring rotates to drive y -> 0
//     x_{k+1} = x_k - sigma_k * (y_k >>> k)
//     y_{k+1} = y_k + sigma_k * (x_k >>> k)
//     z_{k+1} = z_k - sigma_k * ATAN_LUT[k]
//
// Quadrant pre-rotation (stage -1): native CORDIC only converges for
// inputs in [-pi/2, +pi/2] (i.e. x >= 0). If x_in < 0, rotate by
// +-pi/2 first and record the rotation in z_0.
//
// Phase encoding: signed PHASE_W-bit two's complement, with +pi
// mapped to (2^(PHASE_W-1) - 1) and -pi mapped to -2^(PHASE_W-1).
// Same convention the NCO uses, so the phase out of this unit feeds
// straight into a differentiator + downstream NCO without rescaling.
//
// CORDIC gain Kn ~ 1.6467602581... After 16 iterations the magnitude
// output is Kn * sqrt(I^2 + Q^2), not the true magnitude. For FM
// demod the magnitude isn't used (only the phase matters) so this
// gain is documented and accepted rather than compensated. Multiply
// by 1/Kn ~ 0.6072529350... externally if true magnitude is needed.
//
// Internal width: SAMPLE_W + 2 to accommodate the Kn gain plus
// headroom for the pre-rotation negation step.
//
// Bit-exactness: the Python reference model in dv/dsp_models.py
// mirrors this stage-by-stage. Any disagreement in shift direction,
// sign extension, or ATAN_LUT rounding compounds across 16 stages
// into per-sample mismatches that don't converge, so the model and
// RTL match exactly on every stage's (x, y, z) registers.
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module cordic #(
    parameter int unsigned SAMPLE_W       = 16,
    parameter int unsigned SAMPLE_INT_W   = 1,
    parameter int unsigned SAMPLE_FRAC_W  = SAMPLE_W - SAMPLE_INT_W,
    parameter int unsigned PHASE_W        = 16,
    parameter int unsigned ITERATIONS     = 16
) (
    input  wire                          clk,
    input  wire                          rst_n,

    // ---- AXIS slave: complex input ----
    input  wire [2*SAMPLE_W-1:0]         s_axis_tdata,
    input  wire                          s_axis_tvalid,
    output wire                          s_axis_tready,

    // ---- AXIS master: { padding, phase, magnitude } ----
    // TDATA layout, low-bit-first:
    //   [SAMPLE_W+1 : 0]                       magnitude (SAMPLE_W+2 bits, unsigned)
    //   [SAMPLE_W+PHASE_W+1 : SAMPLE_W+2]      phase     (PHASE_W bits, signed)
    //   [TDATA_W-1 : SAMPLE_W+PHASE_W+2]       zero padding to byte-align
    //
    // The total TDATA width is rounded up to the next multiple of 8 so
    // that cocotbext-axi's AxiStreamSource/Sink accept it without the
    // "Bus does not evenly divide into byte lanes" complaint. The pad
    // bits are zero and ignored by consumers.
    output wire [(((PHASE_W + SAMPLE_W + 2) + 7) / 8) * 8 - 1 : 0] m_axis_tdata,
    output wire                              m_axis_tvalid,
    input  wire                              m_axis_tready
);

    // ---- Q-format consistency check ----
    initial begin
        if (SAMPLE_INT_W + SAMPLE_FRAC_W != SAMPLE_W)
            $fatal(1, "cordic: SAMPLE_INT_W (%0d) + SAMPLE_FRAC_W (%0d) != SAMPLE_W (%0d)",
                   SAMPLE_INT_W, SAMPLE_FRAC_W, SAMPLE_W);
        if (ITERATIONS != 16)
            $fatal(1, "cordic: only ITERATIONS=16 supported in v1 (got %0d)", ITERATIONS);
    end

    localparam int INTERNAL_W = SAMPLE_W + 2;     // headroom for Kn gain + pre-rotation
    localparam int Z_W        = PHASE_W;          // phase accumulator width

    // ---- atan LUT ----
    // ATAN_LUT[k] = round(atan(2^-k) * 2^(PHASE_W-1) / pi)
    // For PHASE_W=16:
    //   atan(1.0)        = pi/4   -> 2^14   = 16384
    //   atan(0.5)        ~ 0.4636 ->         ~ 9672
    //   atan(0.25)       ~ 0.2450 ->         ~ 5110
    //   ... saturating to 1 at large k.
    // Generated at elaboration via $atan/$rtoi to match the Python
    // model's float-domain calculation (both use IEEE 754 doubles).
    function automatic logic signed [Z_W-1:0] atan_entry(input int k);
        real angle, value;
        int rounded;
        angle = $atan(2.0 ** (-real'(k)));
        value = angle * (2.0 ** real'(Z_W - 1)) / 3.14159265358979323846;
        if (value >= 0.0) rounded = $rtoi(value + 0.5);
        else              rounded = $rtoi(value - 0.5);
        return Z_W'(rounded);
    endfunction

    logic signed [Z_W-1:0] atan_lut [ITERATIONS];
    initial begin
        for (int k = 0; k < ITERATIONS; k++) atan_lut[k] = atan_entry(k);
    end

    // pi/2 in the phase encoding = 2^(PHASE_W-2).
    localparam logic signed [Z_W-1:0] PI_OVER_2 =  Z_W'(1) <<< (PHASE_W - 2);
    localparam logic signed [Z_W-1:0] NEG_PI_2  = -(Z_W'(1) <<< (PHASE_W - 2));

    // ---- Input unpack ----
    wire signed [SAMPLE_W-1:0] in_i = s_axis_tdata[SAMPLE_W-1:0];
    wire signed [SAMPLE_W-1:0] in_q = s_axis_tdata[2*SAMPLE_W-1:SAMPLE_W];

    // ---- Quadrant pre-rotation (combinational; feeds stage 0 registers) ----
    wire signed [INTERNAL_W-1:0] in_i_ext = {{(INTERNAL_W-SAMPLE_W){in_i[SAMPLE_W-1]}}, in_i};
    wire signed [INTERNAL_W-1:0] in_q_ext = {{(INTERNAL_W-SAMPLE_W){in_q[SAMPLE_W-1]}}, in_q};

    logic signed [INTERNAL_W-1:0] x_pre;
    logic signed [INTERNAL_W-1:0] y_pre;
    logic signed [Z_W-1:0]        z_pre;
    always_comb begin
        if (!in_i[SAMPLE_W-1]) begin
            // x >= 0: no pre-rotation needed.
            x_pre = in_i_ext;
            y_pre = in_q_ext;
            z_pre = '0;
        end else if (!in_q[SAMPLE_W-1]) begin
            // x < 0, y >= 0: second quadrant. Rotate by +pi/2:
            //   new_x = +y, new_y = -x, z = +pi/2
            x_pre =  in_q_ext;
            y_pre = -in_i_ext;
            z_pre =  PI_OVER_2;
        end else begin
            // x < 0, y < 0: third quadrant. Rotate by -pi/2:
            //   new_x = -y, new_y = +x, z = -pi/2
            x_pre = -in_q_ext;
            y_pre =  in_i_ext;
            z_pre =  NEG_PI_2;
        end
    end

    // ---- Pipeline state ----
    // Stage k inputs are registered. valid follows the data through.
    // 17 sets of registers total: stage 0 (post-pre-rotation, pre-iter)
    // through stage 16 (post-final-iter).
    logic signed [INTERNAL_W-1:0] x_reg [ITERATIONS+1];
    logic signed [INTERNAL_W-1:0] y_reg [ITERATIONS+1];
    logic signed [Z_W-1:0]        z_reg [ITERATIONS+1];
    logic                         v_reg [ITERATIONS+1];

    // ---- Output register ----
    logic                                  out_valid_q;
    logic signed [INTERNAL_W-1:0]          out_mag_q;
    logic signed [Z_W-1:0]                 out_phase_q;

    // ---- Handshake ----
    // The pipeline always advances when the output is ready (or empty).
    // Backpressure: if the consumer stalls, the pipeline stalls.
    wire pipeline_advance = !out_valid_q || m_axis_tready;

    assign s_axis_tready = pipeline_advance;
    assign m_axis_tvalid = out_valid_q;
    // Pack {phase, magnitude} with zero padding to the byte-aligned
    // TDATA width. Magnitude is interpreted unsigned by consumers, but
    // inside x_reg it's stored signed (always non-negative
    // post-iteration). Cast to unsigned via the explicit slice.
    localparam int TDATA_W = (((PHASE_W + INTERNAL_W) + 7) / 8) * 8;
    localparam int PAD_W   = TDATA_W - (PHASE_W + INTERNAL_W);
    assign m_axis_tdata  = { {PAD_W{1'b0}},
                             out_phase_q,
                             out_mag_q[INTERNAL_W-1:0] };

    // ---- Pipeline registers ----
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (int k = 0; k <= ITERATIONS; k++) begin
                x_reg[k] <= '0;
                y_reg[k] <= '0;
                z_reg[k] <= '0;
                v_reg[k] <= 1'b0;
            end
            out_valid_q <= 1'b0;
            out_mag_q   <= '0;
            out_phase_q <= '0;
        end else if (pipeline_advance) begin
            // Stage 0: load pre-rotated input.
            x_reg[0] <= x_pre;
            y_reg[0] <= y_pre;
            z_reg[0] <= z_pre;
            v_reg[0] <= s_axis_tvalid;

            // Stages 1..ITERATIONS: shift-add.
            for (int k = 0; k < ITERATIONS; k++) begin
                // sigma = -sign(y). If y < 0 (sign bit set), sigma = +1
                // and we add y_shifted to x; otherwise sigma = -1.
                // sigma_pos here means "sigma is positive", i.e. y < 0.
                automatic logic sigma_pos = y_reg[k][INTERNAL_W-1];
                // Arithmetic shift right by k: sign-extends.
                automatic logic signed [INTERNAL_W-1:0] x_shifted = x_reg[k] >>> k;
                automatic logic signed [INTERNAL_W-1:0] y_shifted = y_reg[k] >>> k;
                if (sigma_pos) begin
                    x_reg[k+1] <= x_reg[k] - y_shifted;
                    y_reg[k+1] <= y_reg[k] + x_shifted;
                    z_reg[k+1] <= z_reg[k] - atan_lut[k];
                end else begin
                    x_reg[k+1] <= x_reg[k] + y_shifted;
                    y_reg[k+1] <= y_reg[k] - x_shifted;
                    z_reg[k+1] <= z_reg[k] + atan_lut[k];
                end
                v_reg[k+1] <= v_reg[k];
            end

            // Output stage.
            out_mag_q   <= x_reg[ITERATIONS];
            out_phase_q <= z_reg[ITERATIONS];
            out_valid_q <= v_reg[ITERATIONS];
        end else if (m_axis_tready && out_valid_q) begin
            out_valid_q <= 1'b0;
        end
    end

endmodule

`default_nettype wire
