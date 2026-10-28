"""Writes font-widths.json: the advance width of every character of the reports' font, as a
fraction of the font size, for the weights the charts use. The chart service measures
text with it, so a label is cut where the PDF would cut it (zrender's fallback table is
for a Helvetica-like font and is off by up to a third on accented text).

Run again only when the fonts in backend/app/reports/fonts change:

    uv run --with fonttools python frontend/chart-renderer/font_widths.py
"""
import json
from pathlib import Path

from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
FONTS = HERE.parents[1] / "backend" / "app" / "reports" / "fonts"
WEIGHTS = {"400": "IBMPlexSans-Regular.ttf", "600": "IBMPlexSans-SemiBold.ttf", "700": "IBMPlexSans-Bold.ttf"}

out = {}
for weight, name in WEIGHTS.items():
    font = TTFont(FONTS / name)
    upem = font["head"].unitsPerEm
    metrics = font["hmtx"].metrics
    out[weight] = {chr(cp): round(metrics[glyph][0] / upem, 4)
                   for cp, glyph in sorted(font.getBestCmap().items()) if cp >= 32}
(HERE / "font-widths.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
print({k: len(v) for k, v in out.items()})
