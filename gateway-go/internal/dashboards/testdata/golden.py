# The cases of the dashboards' checks and the Python answers (services/dashboards.py check):
#   docker run --rm -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/dashboards/testdata:/t" -w /app \
#       tabularia-gateway:latest python /t/golden.py
import base64, copy, json, random

from app.services import dashboards as db
from app.services.report_definition import Problem

PNG = "data:image/png;base64,iVBO\nRw0KGgowMDAw"
BASE = {
    "v": 1, "locale": "it", "timezone": "Europe/Rome", "refresh_seconds": 300,
    "page": {"size": "A4", "orientation": "landscape"},
    "parameters": [{"name": "region", "type": "text", "default": "Nord"}],
    "datasets": [{"name": "orders", "source": {"kind": "datasource", "datasource_id": 3}}],
    "tiles": [
        {"id": "k1", "type": "kpi", "grid": {"x": 0, "y": 0, "w": 3, "h": 2}, "dataset": "orders", "value": "SUM([n])", "title": "Total"},
        {"id": "c1", "type": "chart", "grid": {"x": 3, "y": 0, "w": 6, "h": 4}, "dataset": "orders", "kind": "bar", "category": "[r]",
         "series": [{"value": "SUM([n])"}], "title": "By region", "filters_others": True},
        {"id": "t1", "type": "table", "grid": {"x": 0, "y": 4, "w": 12, "h": 6}, "dataset": "orders", "columns": [{"value": "[r]"}]},
        {"id": "img", "type": "image", "grid": {"x": 9, "y": 0, "w": 1, "h": 1}, "asset": PNG},
        {"id": "tx", "type": "text", "grid": {"x": 10, "y": 0, "w": 2, "h": 1}, "value": "Hello", "title": "T"},
        {"id": "ai", "type": "comment", "grid": {"x": 0, "y": 10, "w": 4, "h": 3}, "reads": ["k1"]},
    ],
}
random.seed(11)
WEIRD = [None, True, 0, 1, -1, 1.5, 12, 13, 59, 60, 86401, "x", "", [], {}, {"a": 1}, "kpi", "image", 4, 2, 1000, 1001, 100, 101]


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
for p in [p for p in paths(BASE) if p]:
    for value in random.sample(WEIRD, 7):
        d = copy.deepcopy(BASE)
        at(d, p[:-1])[p[-1]] = value
        cases.append(d)
    d = copy.deepcopy(BASE)
    parent = at(d, p[:-1])
    if isinstance(parent, dict):
        del parent[p[-1]]
        cases.append(d)
        d = copy.deepcopy(BASE)
        at(d, p[:-1])["x"] = 1
        cases.append(d)
for extra in [[], {}, {"v": 1}, {"v": 1, "tiles": [{"id": "a", "type": "kpi", "grid": {"x": 0, "y": 0, "w": 2, "h": 2}, "dataset": "nope", "value": "1"}]},
              {"v": 1, "tiles": [{"id": "a", "type": "text", "grid": {"x": 0, "y": 0, "w": 2, "h": 1}},
                                 {"id": "b", "type": "text", "grid": {"x": 1, "y": 0, "w": 2, "h": 1}}]},
              {"v": 1, "tiles": [None]}, {"v": 1, "tiles": [{"id": "a", "type": "kpi", "grid": {"x": 0, "y": 0, "w": 2, "h": 2}, "value": "1", "dataset": "d"}],
                                          "datasets": [{"name": "d", "source": {"kind": "view", "view_id": 1}}]},
              {"v": 1, "tiles": [{"id": "c", "type": "chart", "grid": {"x": 0, "y": 0, "w": 4, "h": 4}, "dataset": "d", "series": []}],
               "datasets": [{"name": "d", "source": {"kind": "view", "view_id": 1}}]},
              {"v": 1, "parameters": [{"name": "a", "type": "x"}] * 21}]:
    cases.append(extra)

out = []
for d in cases:
    text = json.dumps(d)
    try:
        checked = db.check(json.loads(text))
        out.append({"ok": json.dumps(checked)})
    except Problem as p:
        s = db._said(p)
        out.append({"problem": db._problem_dict(s, json.loads(text)), "where": s.where})
    except Exception as e:  # noqa: BLE001
        out.append({"error": type(e).__name__})
json.dump([json.dumps(d) for d in cases], open("/t/cases.json", "w"))
json.dump(out, open("/t/golden.json", "w"), ensure_ascii=False, indent=0)
print(len(cases), "cases,", sum(1 for o in out if "problem" in o), "problems,", sum(1 for o in out if "error" in o), "errors")
