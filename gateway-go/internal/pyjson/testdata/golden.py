# The expected output of every case of cases.json, by Python's own json.dumps:
#   docker run -i --rm -v "$PWD/gateway-go/internal/pyjson/testdata:/t" tabularia-gateway:latest python /t/golden.py
import datetime, json

def decode(v):
    if isinstance(v, dict):
        if "obj" in v:
            return {k: decode(x) for k, x in v["obj"]}
        if "f" in v:
            return float(v["f"])
        if "dt" in v:
            return datetime.datetime.fromisoformat(v["dt"])
    if isinstance(v, list):
        return [decode(x) for x in v]
    return v

out = []
for c in json.load(open("/t/cases.json")):
    kw = {"default": str, "ensure_ascii": bool(c.get("ascii")), "sort_keys": bool(c.get("sort"))}
    if c.get("compact"):
        kw["separators"] = (",", ":")
    out.append(json.dumps(decode(c["v"]), **kw))
json.dump(out, open("/t/golden.json", "w"), ensure_ascii=False, indent=0)
