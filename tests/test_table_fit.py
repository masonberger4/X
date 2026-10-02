"""render_table wraps each cell to its own column and shrinks the font so no text spills
into a neighbouring cell or row (draft/chart.py:_fit_table)."""

import pytest

from draft import chart
from draft.chart import Style, Table

pytest.importorskip("matplotlib")

CROWDED = Table(
    title="Phase 3 readouts in second-line NSCLC",
    columns=["Company", "Program", "Median overall survival", "Grade 3+ adverse events"],
    rows=[
        ["Genmab", "acasunlimab plus pembrolizumab", "17.5 months versus 11.2 months", "41%"],
        ["Example Therapeutics", "an anti-PD-1 x CTLA-4 bispecific", "not reached", "28%"],
        ["Other Bio", "x", "14.0 months in the PD-L1 high subgroup only", "35% with two deaths"],
    ],
    note="",
)


def _capture(monkeypatch):
    """Keep the figure render_table draws so the test can measure its text."""
    import matplotlib.pyplot as plt

    seen = {}
    real_close = plt.close

    def close(fig=None):
        seen["fig"] = fig

    monkeypatch.setattr(plt, "close", close)
    return seen, real_close


@pytest.mark.parametrize("font_scale", [1.0, 1.6])
def test_no_cell_text_leaves_its_cell(tmp_path, monkeypatch, font_scale):
    seen, real_close = _capture(monkeypatch)
    chart.render_table(CROWDED, tmp_path / "t.png", style=Style(font_scale=font_scale))
    fig = seen["fig"]
    try:
        renderer = fig.canvas.get_renderer()
        tbl = next(a for ax in fig.axes for a in ax.tables)
        for (r, _c), cell in tbl.get_celld().items():
            if r == 0 or not cell.get_text().get_text().strip():
                continue
            box = cell.get_window_extent(renderer)
            text = cell.get_text().get_window_extent(renderer)
            assert text.x1 <= box.x1 + 1, cell.get_text().get_text()
            assert text.height <= box.height + 1, cell.get_text().get_text()
    finally:
        real_close(fig)


def test_short_table_keeps_the_full_font(tmp_path, monkeypatch):
    seen, real_close = _capture(monkeypatch)
    small = Table(title="t", columns=["Arm", "ORR"], rows=[["A", "40%"], ["B", "22%"]], note="")
    chart.render_table(small, tmp_path / "t.png", style=Style())
    fig = seen["fig"]
    try:
        tbl = next(a for ax in fig.axes for a in ax.tables)
        assert tbl[1, 0].get_text().get_fontsize() == pytest.approx(6.8)
    finally:
        real_close(fig)
