"""
Plot helpers for DSP unit tests.

Each DSP testcase calls :func:`plot_test_result` at the end with the
input samples it drove, the bit-exact reference output (from the
Python model in ``dv/dsp_models.py``), and the RTL output collected
from the BFM. The helper writes a three-panel PNG showing:

  1. Input samples driven into the RTL
  2. Reference (Python model) and RTL outputs overlaid
  3. Diff (RTL − reference). When the test passed bit-exactly this is
     a flat line at zero — boring but correct.

Plots go to ``<repo_root>/build/dsp_plots/<filename>.png``. The build
directory is gitignored; plots are regenerated every run.

If matplotlib isn't installed the helper silently skips — bit-exact
comparison in the test body still asserts pass/fail.

The plots are useful even when the test passes: they let you eyeball
filter shape (impulse response, step response, settling behaviour)
without needing to write the comparison logic in numpy from scratch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence


def _project_root() -> Path:
    """Resolve the project root from this file's location."""
    # dv/dsp_plot.py lives at <root>/dv/dsp_plot.py
    return Path(__file__).resolve().parents[1]


def plot_test_result(
    filename: str,
    title: str,
    inputs: Sequence[int],
    expected: Sequence[int],
    got: Sequence[int],
    *,
    input_label: str = "input samples",
    output_label: str = "output samples",
    input_rate_ratio: float = 1.0,
) -> Path | None:
    """Write a comparison plot to ``build/dsp_plots/<filename>.png``.

    Returns the path written, or ``None`` if plotting was skipped (e.g.
    matplotlib unavailable). The DV does not depend on the return
    value — it's just there so a caller could ``logger.info`` the
    path if it wanted to.

    Arguments:
        filename         basename for the PNG (no directory, no
                         extension — the helper adds ``.png``)
        title            figure suptitle
        inputs           samples driven into the DUT
        expected         reference (Python-model) output samples
        got              RTL output samples (same length as expected
                         when the test passes; may differ on failure)
        input_label      legend label for the input subplot
        output_label     legend label for the output subplot
        input_rate_ratio used only for x-axis scaling of the input
                         plot relative to the output plots (e.g. for
                         a decimator pass ``DECIM`` so the inputs and
                         outputs line up visually on a common cycle
                         axis; for a unity-rate filter leave at 1.0)
    """
    try:
        import matplotlib  # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    out_dir = _project_root() / "build" / "dsp_plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{filename}.png"

    fig, axes = plt.subplots(3, 1, figsize=(11, 7))
    fig.suptitle(title)

    # Input subplot: x-axis is "input sample index" * input_rate_ratio so
    # for a decimator with DECIM=4 the input span lines up with 1/4 the
    # output span.
    x_in = [i * input_rate_ratio for i in range(len(inputs))]
    axes[0].plot(x_in, inputs, ".-", linewidth=0.8, markersize=3,
                 label=input_label, color="tab:gray")
    axes[0].set_ylabel("amplitude (int)")
    axes[0].set_title("input")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best", fontsize="small")

    # Output overlay: model vs RTL on the same axes.
    n = min(len(expected), len(got))
    x_out = list(range(n))
    axes[1].plot(x_out, expected[:n], ".-", linewidth=1.0, markersize=4,
                 label=f"reference (Python model)", color="tab:blue", alpha=0.8)
    axes[1].plot(x_out, got[:n], "x--", linewidth=0.8, markersize=4,
                 label=f"HDL output", color="tab:red", alpha=0.8)
    axes[1].set_ylabel("amplitude (int)")
    axes[1].set_title(output_label)
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best", fontsize="small")

    # Diff: RTL minus model. Zero everywhere = bit-exact match.
    diff = [int(g) - int(e) for g, e in zip(got[:n], expected[:n])]
    max_abs = max((abs(d) for d in diff), default=0)
    axes[2].plot(x_out, diff, ".-", linewidth=0.8, markersize=3,
                 color="tab:purple")
    axes[2].axhline(0, color="black", linewidth=0.5, alpha=0.3)
    axes[2].set_ylabel("HDL − ref")
    axes[2].set_xlabel("output sample index")
    axes[2].set_title(
        f"diff (max |HDL − ref| = {max_abs}; "
        f"{'bit-exact' if max_abs == 0 else 'MISMATCH'})")
    axes[2].grid(True, alpha=0.3)
    # If lengths differ, annotate it on the diff subplot.
    if len(got) != len(expected):
        axes[2].text(0.02, 0.95,
                     f"length mismatch: got={len(got)}, expected={len(expected)}",
                     transform=axes[2].transAxes, verticalalignment="top",
                     fontsize=8, color="red")

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path, dpi=80)
    plt.close(fig)
    return out_path
