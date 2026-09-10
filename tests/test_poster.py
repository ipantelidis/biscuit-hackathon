"""T5: no headline ever leaves its column."""
import json
import re
from pathlib import Path

import models
import tools

COL = {"stacked": 920, "split": 470, "badge": 920}
TEXT = re.compile(r'<text x="(\d+)" y="(\d+)" font-family="[^"]+" font-size="(\d+)"[^>]*>([^<]*)</text>')


def _lines(svg):
    return [(int(m.group(3)), m.group(4)) for m in TEXT.finditer(svg)]


def _assert_fits(spec, layout):
    svg = tools.render_poster_svg({**spec, "layout": layout}, "Nachtfiets")
    for size, text in _lines(svg):
        if size >= 26:  # headline and subline lines (footer labels are smaller)
            assert len(text) * size * tools.CHAR_W <= COL[layout] + 1, (layout, size, text)


def test_all_mock_specs_fit_all_layouts():
    for f in Path("mocks").glob("designer*.json"):
        for a in json.loads(f.read_text())["assets"]:
            for layout in COL:
                _assert_fits(a, layout)


def test_long_headline_fits():
    spec = {"headline": "Amsterdam charges you a theft tax every autumn", "subline": "and nobody has done anything about it until now",
            "palette": {"bg": "#111", "fg": "#fff", "accent": "#f00"}, "glyph": "⚖"}
    for layout in COL:
        _assert_fits(spec, layout)


def test_headline_word_cap():
    a = models.AssetSpec(content_index=0, headline="one two three four five six seven eight nine ten")
    assert len(a.headline.split()) == 8
