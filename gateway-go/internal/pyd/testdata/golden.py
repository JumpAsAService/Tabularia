"""Answers every case of cases.json with the REAL FastAPI + pydantic of the gateway's
image, and writes the answers (status, content type, exact body) to golden.json.gz.

The app is a stand-in with the gateway's own 422 handler (app.main._invalid_request,
imported, not copied) and one route per model: POST /b/<key> takes the model as its
body, /ob/<key> as an optional body; the query and path routes are built from
routes.json. A body that validates answers with model_dump(mode="json") and the
fields the input set, so the conversions are compared too, not only the errors."""
import base64
import gzip
import json
import sys
import typing
from datetime import datetime
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

sys.path.insert(0, "/td")

import logging  # noqa: E402

from fastapi import FastAPI, Path, Query, Response  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

import gen_models  # noqa: E402
import synthetic  # noqa: E402
from app.main import _invalid_request  # noqa: E402
from app.schemas.models import LoginRequest  # noqa: E402

app = FastAPI()
app.add_exception_handler(RequestValidationError, _invalid_request)


def registry() -> dict:
    reg = {f"{m.__module__}.{m.__qualname__}": m for m in gen_models.gateway_models()}
    reg.update({f"syn.{m.__name__}": m for m in gen_models.collect(synthetic.MODELS)})
    return reg


def text(d) -> Response:
    """The answer of a request that validated, written so that it never fails itself
    (NaN, lone surrogates): what is compared is the validation, not this route."""
    try:
        b = json.dumps(d, ensure_ascii=False, allow_nan=True, separators=(",", ":")).encode("utf-8", "surrogatepass")
    except RecursionError:
        b = b'"too deep to write"'
    return Response(b, media_type="application/json")


def dump(body) -> Response:
    if body is None:
        return text({"dump": None, "set": []})
    try:
        d = body.model_dump(mode="json")
    except RecursionError:
        d = "too deep to dump"
    return text({"dump": d, "set": [n for n in type(body).model_fields if n in body.model_fields_set]})


def body_route(model, optional: bool):
    if optional:
        def ep(body: Optional[model] = None):
            return dump(body)
    else:
        def ep(body: model):
            return dump(body)
    return ep


def py_type(spec: str):
    if spec.startswith("opt:"):
        return Optional[py_type(spec[4:])]
    if spec.startswith("lit:"):
        return Literal[tuple(spec[4:].split(","))]
    return {"int": int, "str": str, "bool": bool, "float": float, "datetime": datetime}[spec]


def query_route(params):
    import inspect

    sig = []
    for name, spec, cons, default in params:
        ann = Annotated[py_type(spec), Query(**cons)]
        d = inspect.Parameter.empty if default == "REQUIRED" else default
        sig.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=d))

    def ep(**kw):
        return text({"values": TypeAdapter(dict[str, Any]).dump_python(kw, mode="json")})

    ep.__signature__ = inspect.Signature(sig)
    return ep


def path_route(spec: str):
    def ep(v: Annotated[py_type(spec), Path()]):
        return text({"values": TypeAdapter(dict[str, Any]).dump_python({"v": v}, mode="json")})
    return ep


def combo(pid: int, body: LoginRequest, limit: Annotated[int, Query(ge=1, le=500)] = 50):
    return {"ok": True}


def main() -> None:
    logging.disable(logging.INFO)
    reg = registry()
    for key, model in reg.items():
        app.post(f"/b/{key}")(body_route(model, False))
        app.post(f"/ob/{key}")(body_route(model, True))
    routes = json.load(open("/td/routes.json"))
    for name, params in routes["query"].items():
        app.get(f"/q/{name}")(query_route(params))
    for name, spec in routes["path"].items():
        app.get(f"/p/{name}/{{v}}")(path_route(spec))
    app.post("/combo/{pid}")(combo)

    client = TestClient(app, raise_server_exceptions=False)
    with gzip.open("/td/cases.json.gz", "rt", encoding="utf-8") as fh:
        cases = json.load(fh)
    out = []
    for c in cases:
        kind = c["kind"]
        if kind in ("body", "obody"):
            raw = base64.b64decode(c["body_b64"]) if "body_b64" in c else c["body"].encode("utf-8", "surrogatepass")
            headers = {} if c.get("ct") is None else {"content-type": c["ct"]}
            prefix = "/b/" if kind == "body" else "/ob/"
            r = client.post(prefix + c["model"], content=raw, headers=headers)
        elif kind == "query":
            r = client.get(f"/q/{c['route']}?{c['query']}")
        elif kind == "path":
            r = client.get(f"/p/{c['route']}/{quote(c['path'], safe='')}")
        elif kind == "combo":
            raw = c["body"].encode("utf-8", "surrogatepass")
            r = client.post(f"/combo/{quote(c['path'], safe='')}?{c['query']}", content=raw,
                            headers={"content-type": "application/json"})
        else:
            raise SystemExit(f"unknown kind {kind}")
        out.append({"id": c["id"], "status": r.status_code, "ct": r.headers.get("content-type"),
                    "body_b64": base64.b64encode(r.content).decode()})
    with gzip.open("/td/golden.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(out, fh, indent=0, ensure_ascii=False)
        fh.write("\n")
    print(f"{len(out)} golden answers")


if __name__ == "__main__":
    main()
