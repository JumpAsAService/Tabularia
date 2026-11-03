"""Writes the Go declarations (internal/pyd) of pydantic models, read from the models
themselves so that the Go twin cannot drift from the Python one:

  - gatewaymodels/models_gen.go: every model a gateway route takes as its body
    (collected from the FastAPI app, nested models included);
  - synthetic_gen_test.go: the models of testdata/synthetic.py.

Run inside the gateway image, with the gateway's sources and this folder mounted
(see run.sh). Validators are referenced by name: a Python validator without a Go
twin in VALIDATORS stops the generation."""
import enum
import json
import sys
import types
import typing
from datetime import datetime

import annotated_types as at
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

sys.path.insert(0, "/td")

# Python validator (function __name__) -> Go function, per target
VALIDATORS = {
    "gateway": {
        "_check_new_password": "checkNewPassword",
        "_no_dot_dot": "noDotDot",
        "_short": "shortTemplates",
        "_complete": "apiVariableComplete",
    },
    "synthetic": {
        "_check": "synCheck",
        "_no_dot_dot": "synNoDotDot",
        "_short": "synShort",
        "_whole": "synWhole",
    },
}


def go_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def go_num(v) -> str:
    if isinstance(v, bool):
        raise TypeError("a bool bound")
    if isinstance(v, int):
        return str(v)
    r = repr(float(v))
    return r if ("." in r or "e" in r) else r + ".0"


def go_scalar(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"int64({v})"
    if isinstance(v, float):
        return f"float64({go_num(v)})"
    if isinstance(v, str):
        return go_str(v)
    raise TypeError(f"not a scalar: {v!r}")


class Gen:
    def __init__(self, target: str, pkg: str, names: dict):
        self.target = target
        self.pkg = pkg
        self.names = names  # model class -> Go var name
        self.q = "pyd." if pkg != "pyd" else ""

    def validator(self, fn) -> str:
        name = getattr(fn, "__name__", None)
        go = VALIDATORS[self.target].get(name)
        if go is None:
            raise SystemExit(f"no Go twin for the validator {fn!r}")
        return go

    def constraints(self, expr: str, metadata) -> str:
        q = self.q
        for m in metadata:
            if isinstance(m, FieldInfo):
                expr = self.constraints(expr, m.metadata)
            elif isinstance(m, at.MinLen):
                expr += f".MinLen({m.min_length})"
            elif isinstance(m, at.MaxLen):
                expr += f".MaxLen({m.max_length})"
            elif isinstance(m, at.Ge):
                expr += f".Ge({go_num(m.ge)})"
            elif isinstance(m, at.Gt):
                expr += f".Gt({go_num(m.gt)})"
            elif isinstance(m, at.Le):
                expr += f".Le({go_num(m.le)})"
            elif isinstance(m, at.Lt):
                expr += f".Lt({go_num(m.lt)})"
            elif type(m).__name__ == "Strict":
                if m.strict:
                    expr += ".Strict()"
            elif type(m).__name__ == "_PydanticGeneralMetadata" and "pattern" in m.__dict__:
                expr += f".Pattern({go_str(m.__dict__['pattern'])})"
            elif type(m).__name__ == "AfterValidator":
                expr += f".After({self.validator(m.func)})"
            else:
                raise SystemExit(f"unsupported metadata {m!r}")
        return expr

    def ty(self, a) -> str:
        q = self.q
        origin = typing.get_origin(a)
        args = typing.get_args(a)
        if a is str:
            return f"{q}Str()"
        if a is bool:
            return f"{q}Bool()"
        if a is int:
            return f"{q}Int()"
        if a is float:
            return f"{q}Float()"
        if a is typing.Any or a is object:
            return f"{q}AnyType()"
        if a is datetime:
            return f"{q}Datetime()"
        if a is list:
            return f"{q}List({q}AnyType())"
        if a is dict:
            return f"{q}Dict({q}AnyType())"
        if origin is typing.Annotated:
            return self.constraints(self.ty(args[0]), args[1:])
        if origin in (typing.Union, types.UnionType):
            choices = [x for x in args if x is not type(None)]
            inner = self.ty(choices[0]) if len(choices) == 1 else f"{q}Union({', '.join(self.ty(c) for c in choices)})"
            return f"{q}Optional({inner})" if len(choices) < len(args) else inner
        if origin is list:
            return f"{q}List({self.ty(args[0]) if args else q + 'AnyType()'})"
        if origin is dict:
            if args and args[0] not in (str, typing.Any):
                raise SystemExit(f"dict keys must be str: {a!r}")
            return f"{q}Dict({self.ty(args[1]) if args else q + 'AnyType()'})"
        if origin is typing.Literal:
            return f"{q}Literal({', '.join(go_scalar(x) for x in args)})"
        if isinstance(a, type) and issubclass(a, enum.Enum):
            return f"{q}Enum({', '.join(go_scalar(m.value) for m in a)})"
        if isinstance(a, type) and issubclass(a, BaseModel):
            return f"{self.names[a]}.Type()"
        raise SystemExit(f"unsupported annotation {a!r}")

    def default(self, f: FieldInfo) -> str:
        if f.is_required():
            return ""
        v = f.default_factory() if f.default_factory is not None else f.default
        v = to_jsonable_python(v)
        if v is None or isinstance(v, (bool, int, float, str)):
            return f".Default({go_scalar(v)})"
        return f".DefaultJSON({go_str(json.dumps(v, ensure_ascii=False, separators=(',', ':')))})"

    def model(self, m) -> str:
        q = self.q
        lines = [f"// {self.names[m]} is {m.__module__}.{m.__qualname__}.",
                 f"var {self.names[m]} = {q}NewModel({go_str(m.__name__)},"]
        for name, f in m.model_fields.items():
            expr = self.constraints(self.ty(f.annotation), f.metadata)
            fe = f"{q}F({go_str(name)}, {expr}){self.default(f)}"
            if f.alias and f.alias != name:
                fe += f".Alias({go_str(f.alias)})"
            if f.validation_alias not in (None, f.alias):
                raise SystemExit(f"unsupported validation_alias on {m.__name__}.{name}")
            lines.append(f"\t{fe},")
        tail = ")"
        cfg = m.model_config
        if cfg.get("extra") == "forbid":
            tail += ".ExtraForbid()"
        elif cfg.get("extra") not in (None, "ignore"):
            raise SystemExit(f"unsupported extra on {m.__name__}")
        if cfg.get("populate_by_name") or cfg.get("validate_by_name"):
            tail += ".PopulateByName()"
        dec = m.__pydantic_decorators__
        for d in dec.field_validators.values():
            if d.info.mode != "after":
                raise SystemExit(f"unsupported field_validator mode on {m.__name__}")
            fields = ", ".join(go_str(x) for x in d.info.fields)
            tail += f".FieldValidator({self.validator(d.func)}, {fields})"
        for d in dec.model_validators.values():
            if d.info.mode != "after":
                raise SystemExit(f"unsupported model_validator mode on {m.__name__}")
            tail += f".After({self.validator(d.func)})"
        if dec.validators or dec.root_validators:
            raise SystemExit(f"unsupported decorators on {m.__name__}")
        lines.append(tail)
        return "\n".join(lines)


def collect(roots) -> list:
    seen: list = []

    def visit(a):
        if isinstance(a, type) and issubclass(a, BaseModel):
            if a in seen:
                return
            seen.append(a)
            for f in a.model_fields.values():
                visit(f.annotation)
            return
        for x in typing.get_args(a):
            visit(x)

    for r in roots:
        visit(r)
    return seen


def gateway_models() -> list:
    from app.main import app
    from fastapi.routing import APIRoute

    def walk(rs):
        for r in rs:
            if isinstance(r, APIRoute):
                yield r
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)

    roots = []

    def deps(d):
        for p in d.body_params:
            roots.append(p.field_info.annotation)
        for s in d.dependencies:
            deps(s)

    for r in walk(app.routes):
        deps(r.dependant)
    return collect(roots)


def go_names(models, prefer_module: str) -> dict:
    by_name: dict = {}
    for m in models:
        by_name.setdefault(m.__name__, []).append(m)
    names = {}
    for m in models:
        if len(by_name[m.__name__]) == 1 or m.__module__ == prefer_module:
            names[m] = m.__name__
        else:
            mod = m.__module__.rsplit(".", 1)[-1]
            names[m] = "".join(p.capitalize() for p in mod.split("_")) + m.__name__
    return names


def write(path: str, pkg: str, header: str, gen: Gen, models, registry_var: str, key) -> None:
    out = [f"// Code generated by testdata/gen_models.py; DO NOT EDIT.", "", header, f"package {pkg}", ""]
    if gen.q:
        out += ['import "github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"', ""]
    for m in sorted(models, key=lambda m: gen.names[m]):
        out += [gen.model(m), ""]
    out.append(f"// {registry_var} maps a model's Python path to its declaration.")
    out.append(f"var {registry_var} = map[string]*{gen.q}Model{{")
    for m in sorted(models, key=key):
        out.append(f"\t{go_str(key(m))}: {gen.names[m]},")
    out.append("}")
    with open(path, "w") as fh:
        fh.write("\n".join(out) + "\n")


def main() -> None:
    gw = gateway_models()
    g = Gen("gateway", "gatewaymodels", go_names(gw, "app.schemas.models"))
    write("/pyd/gatewaymodels/models_gen.go", "gatewaymodels",
          "// Package gatewaymodels declares, for internal/pyd, the request bodies of the\n"
          "// Python gateway's routes: one variable per pydantic model.",
          g, gw, "Registry", lambda m: f"{m.__module__}.{m.__qualname__}")

    import synthetic
    syn = collect(synthetic.MODELS)
    names = {m: "syn" + m.__name__ for m in syn}
    s = Gen("synthetic", "pyd_test", names)
    write("/pyd/synthetic_gen_test.go", "pyd_test", "", s, syn, "synRegistry", lambda m: f"syn.{m.__name__}")
    print(f"{len(gw)} gateway models, {len(syn)} synthetic models")


if __name__ == "__main__":
    main()
