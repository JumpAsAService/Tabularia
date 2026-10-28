"""Pixel-perfect reports, the engine's side (docs/specs/reports.md).

    expressions.py  the expression language: parser, checker, evaluator (never eval)
    formats.py      numbers and dates in five languages, and as Excel formats
    definition.py   the definition (`v: 1`) read and checked, item by item
    data.py         the datasets read as the gateway resolved them, the parameters
    frames.py       the datasets in columns, and what DuckDB computes over their rows
    sql.py          the expressions as DuckDB's SQL, with the interpreter's semantics
    model.py        the render model: bands, tables, groups, totals, matrices, KPIs
    charts.py       the charts' ECharts options and the chart service
    template.typ    the fixed Typst template; typst_child.py and pdf.py lay it out
    xlsx.py, email_html.py   Excel, and the email's body as MHTML
    render.py       one render from the definition to the file
"""
