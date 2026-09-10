"""T5: no headline ever leaves its column."""
import json
import re
from pathlib import Path

import models
import tools

COL = {"photo": 936, "split": 936, "frame": 968}
TEXT = re.compile(r'<text x="(\d+)" y="(\d+)" font-family="[^"]+" font-size="(\d+)"[^>]*>([^<]*)</text>')


def _lines(svg):
    return [(int(m.group(3)), m.group(4)) for m in TEXT.finditer(svg)]


PHOTO = {"path": str(Path("static/iris.jpg").resolve()), "credit": "test", "source_url": ""}


def _assert_fits(spec, layout, photo=PHOTO):
    svg = tools.render_poster_svg({**spec, "layout": layout}, "Nachtfiets", photo)
    for size, text in _lines(svg):
        if size >= 24 and "letter-spacing" not in text:  # headline and subline lines
            assert len(text) * size * tools.CHAR_W <= COL[layout] + 1, (layout, size, text)


def test_all_mock_specs_fit_all_layouts():
    for f in Path("mocks").glob("designer*.json"):
        for a in json.loads(f.read_text())["assets"]:
            for layout in COL:
                _assert_fits(a, layout)
                _assert_fits(a, layout, photo=None)  # flat fallback without a photo


def test_long_headline_fits():
    spec = {"headline": "Amsterdam charges you a theft tax every autumn", "subline": "and nobody has done anything about it until now",
            "palette": {"bg": "#111", "fg": "#fff", "accent": "#f00"}}
    for layout in COL:
        _assert_fits(spec, layout)


def test_seven_word_headline_is_rejected():
    import pytest
    bad = {"assets": [{"content_index": 0, "headline": "one two three four five six seven", "subline": "",
                       "palette": {"bg": "#111", "fg": "#fff", "accent": "#f00"}, "layout": "stacked", "glyph": "x", "alt_text": ""}],
           "message_to_team": "m"}
    with pytest.raises(Exception):
        models.validate("designer", bad)
    ok = {**bad, "assets": [{**bad["assets"][0], "headline": "one two three four five six"}]}
    assert models.validate("designer", ok)["assets"][0]["headline"] == "one two three four five six"
