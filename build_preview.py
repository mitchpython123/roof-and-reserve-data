#!/usr/bin/env python3
"""Build widget/preview.html: the widget on a plain page, pointed at the sample data file.

    python3 scripts/make_sample.py
    python3 scripts/build_preview.py
    cd widget && python3 -m http.server 8000      # then open http://localhost:8000/preview.html

(Browsers won't load the data file if you just double-click preview.html, so use the
little local web server above.)
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
widget = open(os.path.join(ROOT, "widget", "widget.html"), encoding="utf-8").read()
widget = widget.replace("REPLACE_WITH_DATA_URL", "sample-nem-latest.json")

page = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    "<title>Widget preview</title>\n"
    '<style>body{margin:0;padding:16px;background:#F7F9FA;color:#1B2A30;font-family:system-ui,sans-serif}</style>\n'
    "</head><body>\n" + widget + "\n</body></html>\n"
)
out = os.path.join(ROOT, "widget", "preview.html")
open(out, "w", encoding="utf-8").write(page)
print("wrote", out)
