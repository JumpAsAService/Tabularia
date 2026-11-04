# The cases of the definitions' checks and the Python answers, by services/report_definition.py
# itself (and services/dashboards.py for the dashboards' cases):
#   docker run --rm -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/reportdef/testdata:/t" \
#       tabularia-gateway:latest python /t/golden.py
# Each case: a base definition, mutated one way. The answer: the definition as checked (json.dumps),
# or the problem (as_dict, where), or the name of the exception.
import base64, copy, json, random

from app.services import report_definition as rd

PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
SVG_OK = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect x="1" y="1" width="5" height="5" fill="url(#g)"/></svg>'
SVGS = [SVG_OK,
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="http://x/y.png"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>rect { fill: url(http://evil) }</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@im port x</style></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"></svg><svg/>',
        '<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"><inkscape:foo/><g/></svg>',
        '<html/>', '<!DOCTYPE svg><svg/>', '  <svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://other"><x:y/></svg>',
        '<svg><a href="#x"/></svg>', '<svg><use href="#x" style="background: url( #a )"/></svg>',
        '<svg><text>&amp; &lt;</text></svg>', '<svg><text>&nbsp;</text></svg>', 'hello <svg/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>a{}</style><!-- c --></svg>',
        '<svg><image href="data:image/png;base64,AAAA"/></svg>', '<svg><rect ONCLICK="x"/></svg>']

BASE = {
    "v": 1,
    "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12]},
    "locale": "it", "timezone": "Europe/Rome",
    "theme": {"font": "IBM Plex Sans", "size": 9, "color": "#1f2933", "accent": "#3b5bdb"},
    "parameters": [
        {"name": "region", "label": "Region", "type": "text", "default": "Nord", "choices": {"values": ["Nord", "Sud"]}},
        {"name": "since", "type": "date", "default": "{today-30}"},
        {"name": "top", "type": "number", "multi": True, "default": [1, 2.5]},
        {"name": "agent", "type": "text", "required": False, "choices": {"dataset": "orders", "value": "[agent]", "label": "[agent_name]"}},
        {"name": "flag", "type": "boolean", "default": True},
    ],
    "datasets": [
        {"name": "orders", "source": {"kind": "datasource", "datasource_id": 3,
                                      "filters": [{"column": "region", "op": "=", "value": "{region}"},
                                                  {"column": "n", "op": "in", "value": [1, 2]}]},
         "fields": [{"name": "region", "type": "string"}], "calculated": [{"name": "x", "expr": "[a]+1"}]},
        {"name": "q", "source": {"kind": "sql", "sql": "SELECT {region}", "connection_id": 4}},
        {"name": "v", "source": {"kind": "view", "view_id": 2}},
        {"name": "s", "source": {"kind": "sql_script", "script_id": 9}},
    ],
    "sections": {
        "page_header": {"height": 18, "on_first_page": True, "items": [
            {"id": "logo", "type": "image", "x": 0, "y": 0, "w": 30, "h": 10, "asset": PNG, "fit": "contain"},
            {"id": "title", "type": "text", "x": 40, "y": 0, "w": 100, "h": 10, "value": "Report", "style": {"size": 14, "weight": 700}}]},
        "body": {"items": [
            {"id": "k1", "type": "kpi", "x": 0, "y": 0, "w": 50, "h": 20, "dataset": "orders", "value": "SUM([n])",
             "sparkline": {"category": "[d]", "value": "SUM([n])"}},
            {"id": "c1", "type": "chart", "x": 60, "y": 0, "w": 100, "h": 20, "dataset": "orders", "kind": "bar",
             "category": "[region]", "series": [{"value": "SUM([n])", "as": "bar", "axis": "left"}],
             "options": {"stacked": False, "sort": "desc", "top": 10, "bands": [1, 2.5], "target": 5}},
            {"id": "t1", "type": "table", "x": 0, "y": 30, "w": 186, "h": 30, "dataset": "orders",
             "columns": [{"header": "R", "value": "[region]", "width": 40, "align": "left",
                          "rules": [{"when": "[n] > 1", "style": {"color": "#ff0000", "border": {"width": 1, "color": "#000000"}}}]}],
             "groups": [{"by": "[region]", "header": True, "page_break": "between"}], "sort": [{"by": "[n]", "dir": "desc"}],
             "interactive": {"sort": True}},
            {"id": "m1", "type": "table", "x": 0, "y": 70, "w": 186, "h": 30, "dataset": "orders",
             "matrix": {"columns_by": ["[a]", "[b]"], "value": "SUM([n])", "width": 20}},
            {"id": "l1", "type": "line", "x": 10, "y": 110, "w": -5, "h": 0},
            {"id": "r1", "type": "rect", "x": 0, "y": 120, "w": 10, "h": 10, "style": {"background": "#eeeeee", "radius": 2}},
            {"id": "ai", "type": "comment", "x": 0, "y": 140, "w": 100, "h": 30, "reads": ["k1", "c1"], "instruction": "Say."},
        ]},
        "page_footer": {"height": 10, "on_last_page": True, "items": []},
    },
}

random.seed(7)
WEIRD = [None, True, False, 0, 1, -1, 1.5, 0.05, 1e300, "x", "", " ", [], [1], {}, {"a": 1}, 400, 400.0, "#ffffff\n", 2000, 10.25,
         "Europe/Rome", "Local", "UTC", "utc", "a\n", "{today}", "{region}", 12345678901234567890, "normal", "A4", "landscape"]


def paths(node, prefix=()):
    yield prefix
    if isinstance(node, dict):
        for k, v in node.items():
            yield from paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from paths(v, prefix + (i,))


def at(node, path):
    for p in path:
        node = node[p]
    return node


cases = []
all_paths = [p for p in paths(BASE) if p]
for p in all_paths:
    for value in random.sample(WEIRD, 6):
        d = copy.deepcopy(BASE)
        at(d, p[:-1])[p[-1]] = value
        cases.append(d)
    d = copy.deepcopy(BASE)
    parent = at(d, p[:-1])
    if isinstance(parent, dict):
        del parent[p[-1]]
        cases.append(d)
        d = copy.deepcopy(BASE)
        at(d, p[:-1])["zz_unknown"] = 1
        cases.append(d)
for svg in SVGS:
    d = copy.deepcopy(BASE)
    d["sections"]["page_header"]["items"][0]["asset"] = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    cases.append(d)
for asset in ["data:image/png;base64,QUJD", "data:image/png;base64,QUI=", "data:image/jpeg;base64,/9j/", "http://x",
              "data:image/png;base64,iVBO\n Rw0K GgoA", "data:image/gif;base64,R0lG", PNG + "\n"]:
    d = copy.deepcopy(BASE)
    d["sections"]["page_header"]["items"][0]["asset"] = asset
    cases.append(d)
for extra in [{"v": 1}, {"v": True}, {"v": 1.0}, {"v": 2}, {}, {"v": 1, "page": {"size": {"w": 100, "h": 60}, "margins": [20, 20, 20, 20]}},
              {"v": 1, "page": {"size": [1, 2]}}, {"v": 1, "page": {"size": {"w": 300, "h": 200.05}}},
              {"v": 1, "sections": {"body": {"items": [{"id": "a", "type": "table", "x": 0, "y": 0, "w": 10, "h": 10, "dataset": "x"}]}}},
              {"v": 1, "datasets": [{"name": "a", "source": {"kind": "sql", "sql": "x", "engine": "duckdb"}}],
               "sections": {"body": {"items": [{"id": "t", "type": "table", "x": 0, "y": 0, "w": 10, "h": 10, "dataset": "a", "columns": [{"value": "[x]"}]},
                                               {"id": "u", "type": "text", "x": 50, "y": 5, "w": 10, "h": 10}]}}},
              {"v": 1, "parameters": [{"name": "a", "type": "number", "default": "١٢"}]},
              {"v": 1, "parameters": [{"name": "a", "type": "number", "default": "1e3"}, {"name": "b", "type": "number", "default": " +7 "}]},
              {"v": 1, "parameters": [{"name": "a", "type": "date", "default": "2026-02-30"}]},
              {"v": 1, "parameters": [{"name": "a", "type": "number", "default": 1e16}]},
              {"v": 1, "parameters": [{"name": "a", "type": "boolean", "default": "YES", "choices": {"values": [True]}}]},
              {"v": 1, "parameters": [{"name": "a", "type": "number", "default": 2, "choices": {"values": ["2.0", 3]}}]},
              {"v": 1, "parameters": [{"name": "a", "type": "text", "choices": {"dataset": ["x"], "value": "[a]"}}]},
              {"v": 1, "parameters": [{"name": "today", "type": "text"}]},
              {"v": 1, "parameters": [{"name": "a", "type": "text"}, {"name": "a", "type": "text"}]},
              ]:
    cases.append(extra)

out = []
for d in cases:
    text = json.dumps(d)
    try:
        checked = rd.check(json.loads(text))
        out.append({"ok": json.dumps(checked)})
    except rd.Problem as p:
        out.append({"problem": p.as_dict(json.loads(text)), "where": p.where})
    except Exception as e:  # noqa: BLE001
        out.append({"error": type(e).__name__})
json.dump([json.dumps(d) for d in cases], open("/t/cases.json", "w"))
json.dump(out, open("/t/golden.json", "w"), ensure_ascii=False, indent=0)
print(len(cases), "cases,", sum(1 for o in out if "problem" in o), "problems,", sum(1 for o in out if "error" in o), "errors")
