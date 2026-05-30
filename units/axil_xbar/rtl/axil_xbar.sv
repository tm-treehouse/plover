// =============================================================================
// axil_xbar.sv
//
// 1-master to N-slaves AXI4-Lite decoder. Routes each transaction to the
// matching downstream slave by address; returns DECERR for unmapped
// addresses. Read and write paths are independent — a read to slave A can
// be in flight while a write to slave B is in flight.
//
// Despite the name, this is a *decoder*, not a full crossbar (the latter
// would also arbitrate among multiple masters). The interface is set up so
// upgrading to a real crossbar later is a contained change.
//
// Address map
// -----------
// For each slave i in 0..N-1, a transaction with address A is routed to
// that slave if (A & SLAVE_MASK[i]) == SLAVE_BASE[i]. With 4KB pages, set
// SLAVE_MASK[i] = 32'hFFFF_F000 and SLAVE_BASE[i] to the page address.
//
// The full 32-bit address is forwarded to the slave; the slave's own
// address width determines how many low bits it actually uses.
//
// Register stages
// ---------------
// Optional registered stages on each AXI-Lite channel for timing closure.
//   INPUT_REG_STAGES   — flops on master-side channels, before decode.
//   OUTPUT_REG_STAGES  — flops on slave-side channels, after decode.
// Stages are AXI-Lite-compliant skid buffers, so adding them costs latency
// but preserves throughput.
//
// Implementation
// --------------
// Two small FSMs (write_state, read_state) hold the in-flight target per
// channel, so the decode result computed at AW/AR-accept time persists
// until the matching B/R returns. DECERR for unmapped requests is
// synthesised directly from the FSM (no slave involvement).
// =============================================================================
`timescale 1ns / 1ps
`default_nettype none

module axil_xbar #(
    parameter int unsigned         N_SLAVES          = 2,
    parameter int unsigned         ADDR_WIDTH        = 32,
    parameter int unsigned         DATA_WIDTH        = 32,
    parameter int unsigned         INPUT_REG_STAGES  = 0,
    parameter int unsigned         OUTPUT_REG_STAGES = 0,
    parameter logic [31:0]         SLAVE_BASE [N_SLAVES] = '{default: 32'h0},
    parameter logic [31:0]         SLAVE_MASK [N_SLAVES] = '{default: 32'hFFFF_F000}
) (
    input  wire                                 clk,
    input  wire                                 rst_n,

    // ------------- Master-side slave port (the host plugs in here) -------
    input  wire [ADDR_WIDTH-1:0]                s_axil_awaddr,
    input  wire [2:0]                           s_axil_awprot,
    input  wire                                 s_axil_awvalid,
    output wire                                 s_axil_awready,

    input  wire [DATA_WIDTH-1:0]                s_axil_wdata,
    input  wire [(DATA_WIDTH/8)-1:0]            s_axil_wstrb,
    input  wire                                 s_axil_wvalid,
    output wire                                 s_axil_wready,

    output wire [1:0]                           s_axil_bresp,
    output wire                                 s_axil_bvalid,
    input  wire                                 s_axil_bready,

    input  wire [ADDR_WIDTH-1:0]                s_axil_araddr,
    input  wire [2:0]                           s_axil_arprot,
    input  wire                                 s_axil_arvalid,
    output wire                                 s_axil_arready,

    output wire [DATA_WIDTH-1:0]                s_axil_rdata,
    output wire [1:0]                           s_axil_rresp,
    output wire                                 s_axil_rvalid,
    input  wire                                 s_axil_rready,

    // ------------- Downstream master ports (one per slave) ---------------
    output wire [N_SLAVES*ADDR_WIDTH-1:0]       m_axil_awaddr,
    output wire [N_SLAVES*3-1:0]                m_axil_awprot,
    output wire [N_SLAVES-1:0]                  m_axil_awvalid,
    input  wire [N_SLAVES-1:0]                  m_axil_awready,

    output wire [N_SLAVES*DATA_WIDTH-1:0]       m_axil_wdata,
    output wire [N_SLAVES*(DATA_WIDTH/8)-1:0]   m_axil_wstrb,
    output wire [N_SLAVES-1:0]                  m_axil_wvalid,
    input  wire [N_SLAVES-1:0]                  m_axil_wready,

    input  wire [N_SLAVES*2-1:0]                m_axil_bresp,
    input  wire [N_SLAVES-1:0]                  m_axil_bvalid,
    output wire [N_SLAVES-1:0]                  m_axil_bready,

    output wire [N_SLAVES*ADDR_WIDTH-1:0]       m_axil_araddr,
    output wire [N_SLAVES*3-1:0]                m_axil_arprot,
    output wire [N_SLAVES-1:0]                  m_axil_arvalid,
    input  wire [N_SLAVES-1:0]                  m_axil_arready,

    input  wire [N_SLAVES*DATA_WIDTH-1:0]       m_axil_rdata,
    input  wire [N_SLAVES*2-1:0]                m_axil_rresp,
    input  wire [N_SLAVES-1:0]                  m_axil_rvalid,
    output wire [N_SLAVES-1:0]                  m_axil_rready
);

    localparam int unsigned STRB_WIDTH  = DATA_WIDTH / 8;
    localparam int unsigned TGT_W       = $clog2(N_SLAVES+1);
    // Narrower index for actually indexing into N_SLAVES-sized arrays;
    // safe to use only when target != TGT_DECERR.
    localparam int unsigned IDX_W       = (N_SLAVES > 1) ? $clog2(N_SLAVES) : 1;
    localparam [1:0]        RESP_OKAY   = 2'b00;
    localparam [1:0]        RESP_DECERR = 2'b11;
    // Sentinel target value indicating "unmapped" (DECERR). N_SLAVES is one
    // past the last valid slave index.
    localparam [TGT_W-1:0]  TGT_DECERR  = TGT_W'(N_SLAVES);

    // =================================================================
    // Input-side register stages on the master-facing channels.
    // si_* are the post-input-stage versions of s_axil_*.
    // =================================================================
    wire [ADDR_WIDTH-1:0]    si_awaddr;
    wire [2:0]               si_awprot;
    wire                     si_awvalid, si_awready;
    wire [DATA_WIDTH-1:0]    si_wdata;
    wire [STRB_WIDTH-1:0]    si_wstrb;
    wire                     si_wvalid, si_wready;
    wire [1:0]               si_bresp;
    wire                     si_bvalid, si_bready;
    wire [ADDR_WIDTH-1:0]    si_araddr;
    wire [2:0]               si_arprot;
    wire                     si_arvalid, si_arready;
    wire [DATA_WIDTH-1:0]    si_rdata;
    wire [1:0]               si_rresp;
    wire                     si_rvalid, si_rready;

    axil_skid_buffer #(.WIDTH(ADDR_WIDTH+3), .DEPTH(INPUT_REG_STAGES))
        u_aw_in (.clk(clk), .rst_n(rst_n),
                 .s_data({s_axil_awaddr, s_axil_awprot}),
                 .s_valid(s_axil_awvalid), .s_ready(s_axil_awready),
                 .m_data({si_awaddr, si_awprot}),
                 .m_valid(si_awvalid), .m_ready(si_awready));

    axil_skid_buffer #(.WIDTH(DATA_WIDTH+STRB_WIDTH), .DEPTH(INPUT_REG_STAGES))
        u_w_in (.clk(clk), .rst_n(rst_n),
                .s_data({s_axil_wdata, s_axil_wstrb}),
                .s_valid(s_axil_wvalid), .s_ready(s_axil_wready),
                .m_data({si_wdata, si_wstrb}),
                .m_valid(si_wvalid), .m_ready(si_wready));

    axil_skid_buffer #(.WIDTH(2), .DEPTH(INPUT_REG_STAGES))
        u_b_in (.clk(clk), .rst_n(rst_n),
                .s_data(si_bresp), .s_valid(si_bvalid), .s_ready(si_bready),
                .m_data(s_axil_bresp), .m_valid(s_axil_bvalid), .m_ready(s_axil_bready));

    axil_skid_buffer #(.WIDTH(ADDR_WIDTH+3), .DEPTH(INPUT_REG_STAGES))
        u_ar_in (.clk(clk), .rst_n(rst_n),
                 .s_data({s_axil_araddr, s_axil_arprot}),
                 .s_valid(s_axil_arvalid), .s_ready(s_axil_arready),
                 .m_data({si_araddr, si_arprot}),
                 .m_valid(si_arvalid), .m_ready(si_arready));

    axil_skid_buffer #(.WIDTH(DATA_WIDTH+2), .DEPTH(INPUT_REG_STAGES))
        u_r_in (.clk(clk), .rst_n(rst_n),
                .s_data({si_rdata, si_rresp}), .s_valid(si_rvalid), .s_ready(si_rready),
                .m_data({s_axil_rdata, s_axil_rresp}),
                .m_valid(s_axil_rvalid), .m_ready(s_axil_rready));

    // =================================================================
    // Address decode helpers.
    // =================================================================
    function automatic [TGT_W-1:0] decode_target(input [ADDR_WIDTH-1:0] addr);
        decode_target = TGT_DECERR;
        for (int i = 0; i < N_SLAVES; i++) begin
            if (((addr & SLAVE_MASK[i]) == SLAVE_BASE[i])
                && (decode_target == TGT_DECERR))
                decode_target = TGT_W'(i);
        end
    endfunction

    // =================================================================
    // Internal slave-side fan-out signals (so_*). Output-side register
    // stages sit between so_* and m_*.
    // =================================================================
    wire [ADDR_WIDTH-1:0]   so_awaddr  [N_SLAVES];
    wire [2:0]              so_awprot  [N_SLAVES];
    wire [N_SLAVES-1:0]     so_awvalid;
    wire [N_SLAVES-1:0]     so_awready;
    wire [DATA_WIDTH-1:0]   so_wdata   [N_SLAVES];
    wire [STRB_WIDTH-1:0]   so_wstrb   [N_SLAVES];
    wire [N_SLAVES-1:0]     so_wvalid;
    wire [N_SLAVES-1:0]     so_wready;
    wire [1:0]              so_bresp   [N_SLAVES];
    wire [N_SLAVES-1:0]     so_bvalid;
    wire [N_SLAVES-1:0]     so_bready;
    wire [ADDR_WIDTH-1:0]   so_araddr  [N_SLAVES];
    wire [2:0]              so_arprot  [N_SLAVES];
    wire [N_SLAVES-1:0]     so_arvalid;
    wire [N_SLAVES-1:0]     so_arready;
    wire [DATA_WIDTH-1:0]   so_rdata   [N_SLAVES];
    wire [1:0]              so_rresp   [N_SLAVES];
    wire [N_SLAVES-1:0]     so_rvalid;
    wire [N_SLAVES-1:0]     so_rready;

    // =================================================================
    // Write-side FSM.
    //   W_IDLE : wait for AW. On accept, latch target, go to W_DATA.
    //   W_DATA : forward W to target (or swallow on DECERR). Go to W_RESP.
    //   W_RESP : wait for B (real from slave, synthetic on DECERR). On
    //            accept, go to W_IDLE.
    // =================================================================
    typedef enum logic [1:0] { W_IDLE, W_DATA, W_RESP } w_state_e;
    w_state_e          w_state, w_next;
    reg [TGT_W-1:0]    w_target;
    wire [TGT_W-1:0]   aw_decoded = decode_target(si_awaddr);
    wire               w_is_decerr = (w_target == TGT_DECERR);
    // Narrow index, only meaningful when !w_is_decerr.
    wire [IDX_W-1:0]   w_target_idx = w_target[IDX_W-1:0];
    wire [IDX_W-1:0]   aw_decoded_idx = aw_decoded[IDX_W-1:0];

    // AW accepted iff in W_IDLE and we either match a slave (and it's
    // ready) or decode as DECERR (always ready to swallow).
    wire aw_match_ready =
        (aw_decoded != TGT_DECERR) ? so_awready[aw_decoded_idx] : 1'b1;
    assign si_awready = (w_state == W_IDLE) && si_awvalid && aw_match_ready;

    // W accepted iff in W_DATA and either target is ready or we're DECERR
    // (swallow).
    wire w_target_ready =
        w_is_decerr ? 1'b1 : so_wready[w_target_idx];
    assign si_wready = (w_state == W_DATA) && si_wvalid && w_target_ready;

    // AW forwarding: drive only the matched slave's so_awvalid.
    for (genvar i = 0; i < N_SLAVES; i++) begin : g_aw_demux
        assign so_awaddr [i] = si_awaddr;
        assign so_awprot [i] = si_awprot;
        assign so_awvalid[i] =
            (w_state == W_IDLE) && si_awvalid
            && (aw_decoded == TGT_W'(i));
    end

    // W forwarding: drive only the latched target's so_wvalid (and not
    // at all if DECERR).
    for (genvar i = 0; i < N_SLAVES; i++) begin : g_w_demux
        assign so_wdata [i] = si_wdata;
        assign so_wstrb [i] = si_wstrb;
        assign so_wvalid[i] =
            (w_state == W_DATA) && !w_is_decerr
            && (w_target == TGT_W'(i)) && si_wvalid;
    end

    // B return: from the latched target's so_bvalid, or synthetic on DECERR.
    assign si_bvalid =
        (w_state == W_RESP)
        && (w_is_decerr ? 1'b1 : so_bvalid[w_target_idx]);
    assign si_bresp =
        w_is_decerr ? RESP_DECERR
                    : ((w_state == W_RESP) ? so_bresp[w_target_idx] : RESP_OKAY);

    for (genvar i = 0; i < N_SLAVES; i++) begin : g_b_ready
        assign so_bready[i] =
            (w_state == W_RESP) && !w_is_decerr
            && (w_target == TGT_W'(i)) && si_bready;
    end

    always_comb begin
        w_next = w_state;
        case (w_state)
            W_IDLE: if (si_awvalid && si_awready) w_next = W_DATA;
            W_DATA: if (si_wvalid  && si_wready ) w_next = W_RESP;
            W_RESP: if (si_bvalid  && si_bready ) w_next = W_IDLE;
            default: w_next = W_IDLE;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            w_state  <= W_IDLE;
            w_target <= '0;
        end else begin
            w_state <= w_next;
            if (w_state == W_IDLE && si_awvalid && si_awready)
                w_target <= aw_decoded;
        end
    end

    // =================================================================
    // Read-side FSM.
    //   R_IDLE : wait for AR. On accept, latch target, go to R_RESP.
    //   R_RESP : wait for R (real or synthetic). On accept, go to R_IDLE.
    // =================================================================
    typedef enum logic { R_IDLE, R_RESP } r_state_e;
    r_state_e         r_state, r_next;
    reg [TGT_W-1:0]   r_target;
    wire [TGT_W-1:0]  ar_decoded = decode_target(si_araddr);
    wire              r_is_decerr = (r_target == TGT_DECERR);
    wire [IDX_W-1:0]  r_target_idx   = r_target[IDX_W-1:0];
    wire [IDX_W-1:0]  ar_decoded_idx = ar_decoded[IDX_W-1:0];

    wire ar_match_ready =
        (ar_decoded != TGT_DECERR) ? so_arready[ar_decoded_idx] : 1'b1;
    assign si_arready = (r_state == R_IDLE) && si_arvalid && ar_match_ready;

    for (genvar i = 0; i < N_SLAVES; i++) begin : g_ar_demux
        assign so_araddr [i] = si_araddr;
        assign so_arprot [i] = si_arprot;
        assign so_arvalid[i] =
            (r_state == R_IDLE) && si_arvalid
            && (ar_decoded == TGT_W'(i));
    end

    assign si_rvalid =
        (r_state == R_RESP)
        && (r_is_decerr ? 1'b1 : so_rvalid[r_target_idx]);
    assign si_rdata =
        r_is_decerr ? {DATA_WIDTH{1'b0}}
                    : ((r_state == R_RESP) ? so_rdata[r_target_idx] : {DATA_WIDTH{1'b0}});
    assign si_rresp =
        r_is_decerr ? RESP_DECERR
                    : ((r_state == R_RESP) ? so_rresp[r_target_idx] : RESP_OKAY);

    for (genvar i = 0; i < N_SLAVES; i++) begin : g_r_ready
        assign so_rready[i] =
            (r_state == R_RESP) && !r_is_decerr
            && (r_target == TGT_W'(i)) && si_rready;
    end

    always_comb begin
        r_next = r_state;
        case (r_state)
            R_IDLE: if (si_arvalid && si_arready) r_next = R_RESP;
            R_RESP: if (si_rvalid  && si_rready ) r_next = R_IDLE;
            default: r_next = R_IDLE;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            r_state  <= R_IDLE;
            r_target <= '0;
        end else begin
            r_state <= r_next;
            if (r_state == R_IDLE && si_arvalid && si_arready)
                r_target <= ar_decoded;
        end
    end

    // =================================================================
    // Output-side register stages on each downstream slave channel.
    // Per-slave wires for the post-stage signals.
    // =================================================================
    for (genvar i = 0; i < N_SLAVES; i++) begin : g_out_stages
        // AW out
        wire [ADDR_WIDTH-1:0] o_awaddr;
        wire [2:0]            o_awprot;
        wire                  o_awvalid;
        axil_skid_buffer #(.WIDTH(ADDR_WIDTH+3), .DEPTH(OUTPUT_REG_STAGES))
            u_aw_out (.clk(clk), .rst_n(rst_n),
                      .s_data({so_awaddr[i], so_awprot[i]}),
                      .s_valid(so_awvalid[i]), .s_ready(so_awready[i]),
                      .m_data({o_awaddr, o_awprot}),
                      .m_valid(o_awvalid),
                      .m_ready(m_axil_awready[i]));
        assign m_axil_awaddr [i*ADDR_WIDTH +: ADDR_WIDTH] = o_awaddr;
        assign m_axil_awprot [i*3          +: 3]          = o_awprot;
        assign m_axil_awvalid[i]                          = o_awvalid;

        // W out
        wire [DATA_WIDTH-1:0] o_wdata;
        wire [STRB_WIDTH-1:0] o_wstrb;
        wire                  o_wvalid;
        axil_skid_buffer #(.WIDTH(DATA_WIDTH+STRB_WIDTH), .DEPTH(OUTPUT_REG_STAGES))
            u_w_out (.clk(clk), .rst_n(rst_n),
                     .s_data({so_wdata[i], so_wstrb[i]}),
                     .s_valid(so_wvalid[i]), .s_ready(so_wready[i]),
                     .m_data({o_wdata, o_wstrb}),
                     .m_valid(o_wvalid),
                     .m_ready(m_axil_wready[i]));
        assign m_axil_wdata [i*DATA_WIDTH +: DATA_WIDTH] = o_wdata;
        assign m_axil_wstrb [i*STRB_WIDTH +: STRB_WIDTH] = o_wstrb;
        assign m_axil_wvalid[i]                          = o_wvalid;

        // B back (from slave, into decoder)
        wire [1:0] i_bresp;
        wire       i_bvalid;
        axil_skid_buffer #(.WIDTH(2), .DEPTH(OUTPUT_REG_STAGES))
            u_b_back (.clk(clk), .rst_n(rst_n),
                      .s_data(m_axil_bresp[i*2 +: 2]),
                      .s_valid(m_axil_bvalid[i]),
                      .s_ready(m_axil_bready[i]),
                      .m_data(i_bresp),
                      .m_valid(i_bvalid),
                      .m_ready(so_bready[i]));
        assign so_bvalid[i] = i_bvalid;
        assign so_bresp [i] = i_bresp;

        // AR out
        wire [ADDR_WIDTH-1:0] o_araddr;
        wire [2:0]            o_arprot;
        wire                  o_arvalid;
        axil_skid_buffer #(.WIDTH(ADDR_WIDTH+3), .DEPTH(OUTPUT_REG_STAGES))
            u_ar_out (.clk(clk), .rst_n(rst_n),
                      .s_data({so_araddr[i], so_arprot[i]}),
                      .s_valid(so_arvalid[i]), .s_ready(so_arready[i]),
                      .m_data({o_araddr, o_arprot}),
                      .m_valid(o_arvalid),
                      .m_ready(m_axil_arready[i]));
        assign m_axil_araddr [i*ADDR_WIDTH +: ADDR_WIDTH] = o_araddr;
        assign m_axil_arprot [i*3          +: 3]          = o_arprot;
        assign m_axil_arvalid[i]                          = o_arvalid;

        // R back
        wire [DATA_WIDTH-1:0] i_rdata;
        wire [1:0]            i_rresp;
        wire                  i_rvalid;
        axil_skid_buffer #(.WIDTH(DATA_WIDTH+2), .DEPTH(OUTPUT_REG_STAGES))
            u_r_back (.clk(clk), .rst_n(rst_n),
                      .s_data({m_axil_rdata[i*DATA_WIDTH +: DATA_WIDTH],
                               m_axil_rresp[i*2          +: 2]}),
                      .s_valid(m_axil_rvalid[i]),
                      .s_ready(m_axil_rready[i]),
                      .m_data({i_rdata, i_rresp}),
                      .m_valid(i_rvalid),
                      .m_ready(so_rready[i]));
        assign so_rvalid[i] = i_rvalid;
        assign so_rdata [i] = i_rdata;
        assign so_rresp [i] = i_rresp;
    end

endmodule

`default_nettype wire
