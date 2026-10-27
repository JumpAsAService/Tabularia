#!/usr/bin/env python3
"""Generates docs/configuration.md, the reference of every environment variable, from the code.

    python3 .github/scripts/config_reference.py           # rewrite the page
    python3 .github/scripts/config_reference.py --check   # fail if the page is out of date

What comes from the code, so it cannot drift:
  - the settings of the gateway and of the engine (`app/core/config.py` of each): every
    field of the pydantic-settings tree, nested models as `SECTION__FIELD`, with its type,
    default, bounds (`Field(ge=…, le=…)`, `Literal[…]`) and whether it is a secret;
  - the variables both apps read directly (`os.getenv`, `os.environ.get`, `os.environ[…]`);
  - the frontend's runtime variables (`process.env.NUXT_…` in nuxt.config.ts);
  - the variables of the development compose file (`${NAME:-default}`) and of
    infrastructure/.env.example, and the names the Helm chart sets, with the values.yaml
    key each one comes from;
  - which settings an administrator can also change from the panel, and which of them
    apply only when their components restart (`Item(…)` in backend/app/core/panel_settings.py
    and gateway/app/core/overrides.py).

What is written by hand, in config_reference.toml next to this file: the one-line
explanation of each variable, the section titles and their introductions, and the notes
on what must change in production. --check fails when the code has a variable without an
explanation, when an explanation is left for a variable that no longer exists, and when the
page differs from what this script writes.

The settings are read with `ast`, not imported: the gateway and the engine live in
different images with different dependencies, and this check runs where neither is
installed (a bare Python 3.11+, in CI and on a laptop).
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PAGE = "docs/configuration.md"
TEXTS_FILE = ".github/scripts/config_reference.toml"
BACKEND_CONFIG = "backend/app/core/config.py"
GATEWAY_CONFIG = "gateway/app/core/config.py"
BACKEND_APP = "backend/app"
GATEWAY_APP = "gateway/app"
PANEL = "backend/app/core/panel_settings.py"
GATEWAY_PANEL = "gateway/app/core/overrides.py"
COMPOSE = "infrastructure/docker-compose.yml"
ENV_EXAMPLE = "infrastructure/.env.example"
HELM_TEMPLATES = "infrastructure/helm/tabularia/templates"
NUXT_CONFIG = "frontend/nuxt.config.ts"
# what the page is generated from (the tests copy these into a throwaway tree)
SOURCES = [BACKEND_APP, GATEWAY_APP, COMPOSE, ENV_EXAMPLE, HELM_TEMPLATES, NUXT_CONFIG]

FIRST_LINE = "<!-- {% raw %} GitHub Pages: no Liquid in this page -->"
LAST_LINE = "<!-- {% endraw %} -->"

MISSING = object()  # a field without a default: the variable must be set


class Unevaluable(Exception):
    pass


@dataclass
class Variable:
    name: str
    readers: set[str] = field(default_factory=set)  # gateway, engine, frontend, compose, helm
    type: str = ""
    default: object = MISSING
    default_source: str = ""  # the expression, when it cannot be worked out statically
    secret: bool = False
    from_settings: bool = False  # a field of a settings class (vs a direct read)
    order: int = 0
    compose_default: str | None = None
    compose_required: bool = False
    helm: str | None = None  # values.yaml key, or "" when the chart computes the value
    helm_secret: bool = False
    panel: str = ""  # the panel's category, when an administrator can also change it there
    panel_restart: str = ""  # the components that read it only when they start, from the panel


@dataclass
class Found:
    variables: dict[str, Variable]
    errors: list[str]


# ── Reading Python ───────────────────────────────────────────────────────────


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def evaluate(node: ast.AST | None):
    """The value of a default written as a literal, arithmetic, SecretStr("…") or a
    lambda returning one of those. Anything else raises Unevaluable."""
    if node is None:
        raise Unevaluable
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -evaluate(node.operand)
    if isinstance(node, ast.BinOp):
        a, b = evaluate(node.left), evaluate(node.right)
        ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
               ast.Div: lambda: a / b, ast.FloorDiv: lambda: a // b, ast.Pow: lambda: a ** b}
        if type(node.op) in ops:
            return ops[type(node.op)]()
        raise Unevaluable
    if isinstance(node, (ast.List, ast.Tuple)):
        return [evaluate(e) for e in node.elts]
    if isinstance(node, ast.Call) and _name(node.func) in ("SecretStr", "str", "int", "float") \
            and len(node.args) == 1 and not node.keywords:
        value = evaluate(node.args[0])
        return {"SecretStr": str, "str": str, "int": int, "float": float}[_name(node.func)](value)
    if isinstance(node, ast.Lambda) and not node.args.args:
        return evaluate(node.body)
    raise Unevaluable


def describe_type(ann: ast.AST) -> tuple[str, bool]:
    """(readable type, is a secret)."""
    simple = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
              "SecretStr": "string"}
    if isinstance(ann, ast.Name):
        return simple.get(ann.id, ann.id), ann.id == "SecretStr"
    if isinstance(ann, ast.Subscript):
        outer = _name(ann.value)
        if outer == "Optional":
            return describe_type(ann.slice)
        if outer == "Literal":
            items = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            return "one of " + ", ".join(f"`{evaluate(i)}`" for i in items), False
        if outer == "list":
            inner, _ = describe_type(ann.slice)
            return f"JSON list of {inner}s", False
    return ast.unparse(ann), False


def describe_bounds(kind: str, bounds: dict) -> str:
    def num(v):
        return f"{v:g}" if isinstance(v, float) else f"{v:,}" if abs(v) >= 10_000 else str(v)

    low = ("≥ " + num(bounds["ge"])) if "ge" in bounds else ("> " + num(bounds["gt"])) if "gt" in bounds else ""
    high = ("≤ " + num(bounds["le"])) if "le" in bounds else ("< " + num(bounds["lt"])) if "lt" in bounds else ""
    if "ge" in bounds and "le" in bounds:
        return f"{kind}, {num(bounds['ge'])}–{num(bounds['le'])}"
    parts = [p for p in (low, high) if p]
    return f"{kind}, " + " and ".join(parts) if parts else kind


def settings_fields(path: Path, reader: str, variables: dict[str, Variable], errors: list[str]) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    roots = [c for c in classes.values() if any(_name(b) == "BaseSettings" for b in c.bases)]
    if len(roots) != 1:
        errors.append(f"{path}: expected one BaseSettings class, found {len(roots)}")
        return
    root = roots[0]
    for stmt in root.body:  # the env names below assume `__` and no prefix
        if isinstance(stmt, ast.Assign) and any(_name(t) == "model_config" for t in stmt.targets):
            kws = {k.arg: k.value for k in getattr(stmt.value, "keywords", [])}
            if "env_prefix" in kws or evaluate(kws.get("env_nested_delimiter")) != "__":
                errors.append(f"{path}: model_config must use env_nested_delimiter='__' and no env_prefix")

    def walk(cls: ast.ClassDef, prefix: str) -> None:
        for stmt in cls.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            ann = stmt.annotation
            if isinstance(ann, ast.Name) and ann.id in classes:
                walk(classes[ann.id], f"{prefix}{stmt.target.id.upper()}__")
                continue
            name = f"{prefix}{stmt.target.id.upper()}"
            kind, secret = describe_type(ann)
            default_node, bounds = stmt.value, {}
            if isinstance(stmt.value, ast.Call) and _name(stmt.value.func) == "Field":
                kws = {k.arg: k.value for k in stmt.value.keywords}
                bounds = {k: evaluate(kws[k]) for k in ("ge", "gt", "le", "lt") if k in kws}
                if stmt.value.args:
                    default_node = stmt.value.args[0]
                else:
                    default_node = kws.get("default", kws.get("default_factory"))
            var = variables.setdefault(name, Variable(name, order=len(variables)))
            var.readers.add(reader)
            if var.from_settings and reader == "gateway":
                continue  # the same field in both apps: the engine's description stands
            var.from_settings = True
            var.type = describe_bounds(kind, bounds)
            var.secret = var.secret or secret
            try:
                var.default = evaluate(default_node) if default_node is not None else MISSING
                var.default_source = ""
            except Unevaluable:
                var.default, var.default_source = MISSING, ast.unparse(default_node)

    walk(root, "")


ENV_GETTERS = ("os.getenv", "os.environ.get", "getenv", "environ.get")  # also after `from os import …`


def direct_reads(app_dir: Path, reader: str, variables: dict[str, Variable]) -> None:
    """os.getenv("X", default), os.environ.get("X", default), os.environ["X"] (reads only)."""
    for path in sorted(app_dir.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "environ" not in source and "getenv" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            name, default = None, MISSING
            if isinstance(node, ast.Call) and _name(node.func) in ENV_GETTERS \
                    and node.args and isinstance(node.args[0], ast.Constant):
                name = node.args[0].value
                if len(node.args) > 1:
                    try:
                        default = evaluate(node.args[1])
                    except Unevaluable:
                        default = MISSING
            elif isinstance(node, ast.Subscript) and _name(node.value) in ("os.environ", "environ") \
                    and isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant):
                name = node.slice.value
            if not isinstance(name, str):
                continue
            var = variables.setdefault(name, Variable(name, order=len(variables)))
            var.readers.add(reader)
            if var.from_settings:
                continue
            wrapper = parents.get(node)
            cast = _name(wrapper.func) if isinstance(wrapper, ast.Call) else ""
            var.type = var.type or {"int": "integer", "float": "number"}.get(cast, "string")
            if default is not MISSING:
                var.default = {"int": int, "float": float}.get(cast, lambda v: v)(default)


def panel_settings(path: Path) -> dict[str, tuple]:
    """Variable → (its category in the panel, the components that read it only when they
    start): `Item("ingest.parquet_row_group_rows", "import", …)`, with `rollout=` and, for a
    variable that is not a settings field, `env_name="CHDB_MAX_MEMORY_USAGE"`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # module constants used as `rollout=` (GW, WORKERS): their tuples of names
    tuples = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Tuple):
            tuples[node.targets[0].id] = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    keys = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) == "Item" and len(node.args) > 1 \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[1], ast.Constant):
            kw = {k.arg: k.value for k in node.keywords}
            name = node.args[0].value.replace(".", "__").upper()
            if isinstance(kw.get("env_name"), ast.Constant) and kw["env_name"].value:
                name = kw["env_name"].value
            restart = []
            if isinstance(kw.get("rollout"), ast.Name):
                restart = tuples.get(kw["rollout"].id, [])
            elif isinstance(kw.get("rollout"), ast.Tuple):
                restart = [e.value for e in kw["rollout"].elts if isinstance(e, ast.Constant)]
            read = None
            if isinstance(kw.get("env_name"), ast.Constant) and kw["env_name"].value:
                kind = kw["kind"].value if isinstance(kw.get("kind"), ast.Constant) else "int"
                default = kw["default"].value if isinstance(kw.get("default"), ast.Constant) else MISSING
                if isinstance(kw.get("default"), ast.BinOp):  # 512 * 1024**2
                    try:
                        default = eval(compile(ast.Expression(kw["default"]), "<default>", "eval"), {})
                    except Exception:  # noqa: BLE001 — shown as written
                        default = MISSING
                read = ({"float": "number"}.get(kind, "integer"), default)
            keys[name] = (node.args[1].value, ", ".join(restart), read)
    return keys


# ── Reading the deployment files ─────────────────────────────────────────────

COMPOSE_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?:(:?[-?])([^}]*))?\}")
ENV_LINE = re.compile(r"^#?([A-Z][A-Z0-9_]*)=(.*)$")
NUXT_VAR = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)(?:\s*\|\|\s*'([^']*)')?")
HELM_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]*):\s*(.*)$")
HELM_NAME = re.compile(r"^\s*- name: ([A-Z][A-Z0-9_]*)\s*$")
HELM_TAG = re.compile(r"\{\{-?\s*(if|with|range|define|end)\b([^}]*?)-?\}\}")
HELM_VALUE = re.compile(r"\$?\.Values\.([A-Za-z0-9_.]+)")


def compose_variables(text: str) -> dict[str, tuple[str | None, bool]]:
    out: dict[str, tuple[str | None, bool]] = {}
    for m in COMPOSE_VAR.finditer(text):
        name, op, rest = m.groups()
        default = rest if op and op.endswith("-") else None
        required = bool(op and op.endswith("?"))
        old = out.get(name, (None, False))
        out[name] = (old[0] if old[0] is not None else default, old[1] or required)
    return out


def helm_variables(templates: Path) -> dict[str, tuple[str, bool]]:
    """name → (values.yaml key or "" when computed by the chart, comes from the Secret)."""
    out: dict[str, tuple[str, bool]] = {}
    for path in sorted(p for p in templates.iterdir() if p.suffix in (".yaml", ".tpl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        stack: list[str | None] = []  # the .Values path of each open `with`, None for other blocks
        for i, line in enumerate(lines):
            with_path = next((p for p in reversed(stack) if p), None)
            key = HELM_KEY.match(line)
            named = HELM_NAME.match(line)
            name, source = None, ""
            if key and not line.lstrip().startswith("#"):
                name, source = key.group(1), key.group(2)
            elif named:
                name = named.group(1)
                source = lines[i + 1] if i + 1 < len(lines) else ""
            if name:
                m = HELM_VALUE.search(source)
                path_ = m.group(1) if m else (with_path if "{{ . " in source else "")
                if name not in out or (path_ and not out[name][0]):
                    out[name] = (path_, path.name == "secret.yaml")
            for tag, rest in HELM_TAG.findall(line):
                if tag == "end":
                    if stack:
                        stack.pop()
                else:
                    m = HELM_VALUE.search(rest) if tag == "with" else None
                    stack.append(m.group(1) if m else None)
    return out


# ── Collecting ───────────────────────────────────────────────────────────────


def collect(root: Path) -> Found:
    variables: dict[str, Variable] = {}
    errors: list[str] = []
    settings_fields(root / BACKEND_CONFIG, "engine", variables, errors)
    settings_fields(root / GATEWAY_CONFIG, "gateway", variables, errors)
    direct_reads(root / BACKEND_APP, "engine", variables)
    direct_reads(root / GATEWAY_APP, "gateway", variables)

    for name, default in NUXT_VAR.findall((root / NUXT_CONFIG).read_text(encoding="utf-8")):
        var = variables.setdefault(name, Variable(name, order=len(variables)))
        var.readers.add("frontend")
        var.type, var.default = "string", default

    for name, (default, required) in compose_variables((root / COMPOSE).read_text(encoding="utf-8")).items():
        var = variables.setdefault(name, Variable(name, order=len(variables)))
        var.readers.add("compose")
        var.compose_default, var.compose_required = default, required
    for line in (root / ENV_EXAMPLE).read_text(encoding="utf-8").splitlines():
        m = ENV_LINE.match(line)
        if m:
            var = variables.setdefault(m.group(1), Variable(m.group(1), order=len(variables)))
            var.readers.add("compose")

    for name, (path_, secret) in helm_variables(root / HELM_TEMPLATES).items():
        var = variables.setdefault(name, Variable(name, order=len(variables)))
        var.readers.add("helm")
        var.helm, var.helm_secret = path_, secret

    for path_, who in ((PANEL, "engine"), (GATEWAY_PANEL, "gateway")):
        for name, (category, restart, read) in panel_settings(root / path_).items():
            if read is not None:
                # a variable read at import through the catalogue (`valore_all_avvio`), not os.getenv
                var = variables.setdefault(name, Variable(name, order=len(variables)))
                var.readers.add(who)
                var.type = var.type or read[0]
                if var.default is MISSING and read[1] is not MISSING:
                    var.default = read[1]
            if name in variables:
                variables[name].panel = category
                variables[name].panel_restart = restart
            else:
                errors.append(f"{path_}: the panel setting {name} is not a setting of the {who}")
    return Found(variables, errors)


def component(var: Variable) -> str:
    apps = var.readers & {"gateway", "engine"}
    if apps == {"gateway", "engine"}:
        return "shared"
    if apps:
        return apps.pop()
    if "frontend" in var.readers:
        return "frontend"
    return "deployment"


# ── Rendering ────────────────────────────────────────────────────────────────


def slug(title: str) -> str:
    s = re.sub(r"[^\w\- ]", "", title.lower().replace("`", ""))
    return s.replace(" ", "-")


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def show_value(name: str, value) -> str:
    if value is None or value == "" or value == []:
        return "empty"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        text = f"`{value}`"
        for unit, size in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2)):
            if "BYTES" in name and value >= size and value % size == 0:
                return f"{text} ({value // size} {unit})"
        return text
    if isinstance(value, list):
        return f"`{json.dumps(value)}`"
    return f"`{value}`"


def is_secret(var: Variable, text: dict) -> bool:
    """SecretStr in the code, a key of the chart's Secret, or marked in the texts."""
    return var.secret or var.helm_secret or bool(text.get("secret"))


def show_default(var: Variable, text: dict) -> str:
    if "default" in text:
        return text["default"]
    if text.get("required"):
        return "**required**"
    if is_secret(var, text):
        if var.default is MISSING or not var.default:
            return "empty"
        return "development value (hidden)"
    if var.default is MISSING:
        if var.compose_default is not None:
            return show_value(var.name, var.compose_default)
        return f"`{var.default_source}`" if var.default_source else "empty"
    return show_value(var.name, var.default)


def show_helm(var: Variable, text: dict) -> str:
    if "helm" in text:
        return text["helm"]
    if var.helm is not None:
        return f"`{var.helm}`" if var.helm else "set by the chart"
    return "`existingSecret`" if is_secret(var, text) else "`extraEnv`"


def restarted(components: str) -> str:
    """«worker, preview-worker» → «workers»: the words of the panel's restart banner."""
    names = {"gateway": "gateway", "orchestrator": "orchestrator", "engine": "engine API", "beat": "scheduler (beat)",
             "worker": "workers", "preview-worker": "workers"}
    seen = []
    for c in components.split(", "):
        n = names.get(c, c)
        if n not in seen:
            seen.append(n)
    return " and ".join(seen)


def describe(var: Variable, text: dict) -> str:
    out = text["text"].strip()
    if text.get("production"):
        out = f"**Production:** {text['production'].strip()} {out}"
    if var.panel:
        # the workers' processes are shown in Admin → Workers, not among the settings
        out += f" Also adjustable in Administration → {'Workers' if var.panel == 'workers' else 'Settings'}"
        out += f", applied after a restart of the {restarted(var.panel_restart)}." if var.panel_restart else "."
    return cell(out)


def render(found: Found, texts: dict) -> str:
    variables, notes = found.variables, texts["var"]
    sections = texts["section"]
    by_component: dict[str, list[Variable]] = {}
    for var in sorted(variables.values(), key=lambda v: v.order):
        by_component.setdefault(component(var), []).append(var)

    def section_of(var: Variable, comp: str) -> str:
        explicit = notes[var.name].get("section")
        if explicit:
            return explicit
        prefix = var.name.split("__", 1)[0] if "__" in var.name else ""
        for s in sections:
            if s["component"] == comp and prefix in s.get("prefixes", []):
                return s["id"]
        return ""

    page = texts["page"]
    out = [FIRST_LINE, f"# {page['title']}", "", page["intro"].strip(), ""]

    counts = {c: len(by_component.get(c, [])) for c in ("shared", "gateway", "engine", "frontend", "deployment")}
    out += ["| Read by | Variables |", "|---|---|"]
    for comp in ("shared", "gateway", "engine", "frontend", "deployment"):
        out.append(f"| [{page['components'][comp]}](#{slug(page['components'][comp])}) | {counts[comp]} |")
    out += [f"| **Total** | **{sum(counts.values())}** |", ""]

    out += [f"## {page['production_title']}", "", page["production_intro"].strip(), ""]
    # what refuses to start first, then the secrets, then the rest
    def urgency(v: Variable):
        return (0 if notes[v.name].get("required") else 1 if is_secret(v, notes[v.name]) else 2, v.order)

    for var in sorted(variables.values(), key=urgency):
        note = notes[var.name]
        if note.get("production"):
            out.append(f"- `{var.name}` — {cell(note['production'])}")
    out.append("")

    header = "| Variable | Type | Default | Helm value | Description |\n|---|---|---|---|---|"

    def row(var: Variable) -> str:
        note = notes[var.name]
        kind = note.get("type") or var.type or "string"
        if note.get("values"):
            kind = f"{kind}; {note['values']}"
        return (f"| `{var.name}` | {cell(kind)} | {cell(show_default(var, note))} | "
                f"{cell(show_helm(var, note))} | {describe(var, note)} |")

    # shared: one table
    title = page["components"]["shared"]
    out += [f"## {title}", "", page["shared_intro"].strip(), "", header]
    out += [row(v) for v in by_component.get("shared", [])]
    out.append("")

    shared_names = {v.name for v in by_component.get("shared", [])}
    for comp in ("gateway", "engine"):
        out += [f"## {page['components'][comp]}", "", page[f"{comp}_intro"].strip(), ""]
        grouped: dict[str, list[Variable]] = {}
        for var in by_component.get(comp, []):
            grouped.setdefault(section_of(var, comp), []).append(var)
        for s in sections:
            if s["component"] != comp or s["id"] not in grouped:
                continue
            rows = grouped[s["id"]]
            prefixes = s.get("prefixes", [])
            rank = {p: i for i, p in enumerate(prefixes)}
            rows.sort(key=lambda v: (rank.get(v.name.split("__", 1)[0], len(rank)), v.order))
            out += [f"### {s['title']}", ""]
            if s.get("intro"):
                out += [s["intro"].strip(), ""]
            also = sorted(n for n in shared_names if n.split("__", 1)[0] in prefixes)
            if also:
                listed = ", ".join(f"`{n}`" for n in also)
                out += [f"Read by both services, so listed under [{page['components']['shared']}]"
                        f"(#{slug(page['components']['shared'])}): {listed}.", ""]
            out += [header] + [row(v) for v in rows] + [""]
            if s.get("outro"):  # a note after the table, with its own heading when it needs one
                out += [s["outro"].strip(), ""]

    out += [f"## {page['components']['frontend']}", "", page["frontend_intro"].strip(), "",
            "| Variable | Default | Helm value | Description |", "|---|---|---|---|"]
    for var in by_component.get("frontend", []):
        note = notes[var.name]
        out.append(f"| `{var.name}` | {cell(show_default(var, note))} | {cell(show_helm(var, note))} | "
                   f"{describe(var, note)} |")
    out.append("")

    out += [f"## {page['components']['deployment']}", "", page["deployment_intro"].strip(), "",
            "| Variable | Default | Where | Description |", "|---|---|---|---|"]
    for var in by_component.get("deployment", []):
        note = notes[var.name]
        where = note.get("where") or ", ".join(
            w for r, w in (("compose", "Compose (`infrastructure/.env`)"), ("helm", "Helm chart"))
            if r in var.readers)
        default = note.get("default") or (
            "**required**" if var.compose_required else
            show_value(var.name, var.compose_default) if var.compose_default is not None else "empty")
        out.append(f"| `{var.name}` | {cell(default)} | {cell(where)} | {describe(var, note)} |")
    out.append("")

    out += [f"## {page['outro_title']}", "", page["outro"].strip(), "", LAST_LINE]
    return "\n".join(out) + "\n"


def validate(found: Found, texts: dict) -> list[str]:
    errors = list(found.errors)
    notes = texts.get("var", {})
    section_ids = {s["id"] for s in texts.get("section", [])}
    for name, var in sorted(found.variables.items()):
        note = notes.get(name)
        if note is None or not str(note.get("text", "")).strip():
            readers = ", ".join(sorted(var.readers))
            errors.append(f"{name} (read by {readers}) has no explanation: add [var.{name}] "
                          f"with text = \"…\" to {TEXTS_FILE}")
            continue
        if note.get("section") and note["section"] not in section_ids:
            errors.append(f"[var.{name}] names an unknown section '{note['section']}'")
    for name in sorted(set(notes) - set(found.variables)):
        errors.append(f"[var.{name}] in {TEXTS_FILE} documents a variable the code no longer has: remove it")
    if errors:
        return errors
    # every gateway/engine variable must land in a section
    for name, var in found.variables.items():
        comp = component(var)
        if comp not in ("gateway", "engine"):
            continue
        prefix = name.split("__", 1)[0] if "__" in name else ""
        if not notes[name].get("section") and not any(
                s["component"] == comp and prefix in s.get("prefixes", []) for s in texts["section"]):
            errors.append(f"{name}: no section of the {comp} lists the prefix '{prefix}' "
                          f"(add it to a [[section]] or give [var.{name}] a section)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the page is out of date, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    texts = tomllib.loads((root / TEXTS_FILE).read_text(encoding="utf-8"))
    found = collect(root)
    errors = validate(found, texts)
    if errors:
        print(f"{PAGE}: the environment variables of the code and their explanations disagree:")
        for e in errors:
            print(f"  - {e}")
        return 1
    page = render(found, texts)
    target = root / PAGE
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != page:
            print(f"{PAGE} is out of date: run python3 {TEXTS_FILE.replace('.toml', '.py')}")
            diff = difflib.unified_diff(current.splitlines(), page.splitlines(), "committed", "generated", lineterm="", n=0)
            for line in list(diff)[:60]:
                print(line)
            return 1
        print(f"{PAGE}: up to date, {len(found.variables)} variables")
        return 0
    target.write_text(page, encoding="utf-8")
    print(f"{PAGE}: written, {len(found.variables)} variables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
