"""Publication-figure text keeps the spaces between plain and italic segments in every renderer."""
from frx.analysis.svg import Canvas


def test_text_preserves_segment_spaces_and_label_boxes():
    c = Canvas(100, 40)
    c.text(5, 20, [("Difference in ", None), ("P", "it"), (" = 1", None)])
    c.label_box(5, 35, "pod created")
    out = c.render()
    assert out.count("<text ") == out.count("xml:space='preserve'") == 2
    assert "<tspan>Difference in </tspan>" in out
    assert out.index("fill='white'", out.index("<rect width")+1) < out.rindex("pod created")   # box drawn before label
