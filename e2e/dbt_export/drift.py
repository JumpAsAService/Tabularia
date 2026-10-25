"""The gateway mirrors what the engine's dbt exporter knows, so that the export plan says before the
download what the engine would refuse (findings #11 and #49, decision 2). This checks, reading the
code (AST only: no import, no stack, no database), that the two sides have not drifted apart:
  · _REQUIRED (gateway) is the `_require(params, "<key>")` calls of each `op_type` branch of the
    engine's `_compile_op`: a step saved half done is a reason in the plan, not a refused download;
  · SEED_MAX_ROWS, the cap on a seed, is the same number on both sides (seed_too_large);
  · _CLICKHOUSE_READS (gateway) is what the ClickHouse target reads: the engine's
    _CH_FEDERATED_ENGINE and the ClickHouse server itself;
  · _FEDERABLE (gateway) is what the federated target attaches (the engine's _DUCK_ATTACH_TYPE);
  · _NATIVE (gateway) is the engine's _NATIVE_ADAPTER (no_native_adapter);
  · every dialect the gateway sends (_DIALECT) is one the engine's ModelSpec.dialect accepts;
  · _NOT_QUOTABLE: what the gateway refuses in a name is what the engine refuses there
    (name_not_quotable), role by role — a source's schema and table, a model's table, a model's schema —
    for every role either side lists. A flat list (one per adapter) has no role: _quotable applies it to
    every name it checks, a native output's schema and table too, so it must match every role.
usage: python3 drift.py [repository root]"""
import ast, os, sys

R = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = f"{R}/backend/app/engine/dbt_export.py"
ROUTE = f"{R}/backend/app/api/routes/dbt.py"
GATEWAY = f"{R}/gateway/app/services/dbt_export.py"
outcomes = []


def verify(name: str, ok: bool, detail: str = "") -> None:
    outcomes.append(bool(ok))
    print(f"  {'ok' if ok else '✗ '}  {name}{'' if ok else ': ' + detail}", flush=True)


def constants(tree: ast.Module) -> dict:
    """The module-level constants written as literals (`X = …` and `X: T = …`)."""
    out = {}
    for n in tree.body:
        target = n.targets[0] if isinstance(n, ast.Assign) and len(n.targets) == 1 else getattr(n, "target", None)
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Name) and n.value is not None:
            try:
                out[target.id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    return out


def branch_types(test: ast.expr) -> list[str]:
    """`op_type == "x"` or `op_type in ("x", "y")` → the op types of the branch."""
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "op_type":
        right = test.comparators[0]
        if isinstance(test.ops[0], ast.Eq) and isinstance(right, ast.Constant):
            return [right.value]
        if isinstance(test.ops[0], ast.In) and isinstance(right, (ast.Tuple, ast.List, ast.Set)):
            return [e.value for e in right.elts if isinstance(e, ast.Constant)]
    return []


def required_by_engine(tree: ast.Module) -> dict[str, set[str]] | None:
    """The `_require(params, "<key>")` calls of each `if op_type …` branch of `_compile_op` (an
    `elif` chain too: each branch keeps its own calls). None when there is no `_compile_op`."""
    function = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_compile_op"), None)
    if function is None:
        return None
    out: dict[str, set[str]] = {}
    for node in function.body:
        while isinstance(node, ast.If):
            keys = {c.args[1].value for body in node.body for c in ast.walk(body)
                    if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_require"
                    and len(c.args) == 2 and isinstance(c.args[1], ast.Constant)}
            for t in branch_types(node.test):
                out.setdefault(t, set()).update(keys)
            node = node.orelse[0] if len(node.orelse) == 1 else None
    return {t: k for t, k in out.items() if k}


def allowed_dialects(tree: ast.Module) -> set[str] | None:
    """The values of `ModelSpec.dialect: Literal[…] | None` in the engine's route."""
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ModelSpec"), None)
    field = next((n for n in (cls.body if cls else []) if isinstance(n, ast.AnnAssign)
                  and isinstance(n.target, ast.Name) and n.target.id == "dialect"), None)
    if field is None:
        return None
    for n in ast.walk(field.annotation):
        if isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "Literal":
            items = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
            return {v.value for v in items if isinstance(v, ast.Constant)}
    return None


def read(path: str) -> ast.Module | None:
    try:
        return ast.parse(open(path).read())
    except (OSError, SyntaxError) as e:
        verify(f"{os.path.relpath(path, R)} is read", False, str(e))
        return None


engine, route, gateway = read(ENGINE), read(ROUTE), read(GATEWAY)
if None in (engine, route, gateway):
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(1)
ce, cr, cg = constants(engine), constants(route), constants(gateway)


def present(side: dict, where: str, *names: str) -> bool:
    missing = [n for n in names if n not in side]
    if missing:
        verify(f"{', '.join(missing)} in {where}", False, "not found as a literal: the check cannot compare it (renamed or moved?)")
    return not missing


expected = required_by_engine(engine)
if expected is None or not expected:
    verify("the engine's _compile_op and its _require calls are found", False, "no _compile_op, or no _require call in it")
elif present(cg, "the gateway", "_REQUIRED"):
    gateway_r = {t: set(k) for t, k in cg["_REQUIRED"].items()}
    different = sorted(t for t in set(gateway_r) | set(expected) if gateway_r.get(t, set()) != expected.get(t, set()))
    verify(f"_REQUIRED = the _require calls of the engine's _compile_op ({len(expected)} step types)", not different,
           "; ".join(f"{t}: gateway {sorted(gateway_r.get(t, ()))} / engine {sorted(expected.get(t, ()))}" for t in different))

if present(cg, "the gateway", "SEED_MAX_ROWS") and present(ce, "the engine", "SEED_MAX_ROWS"):
    verify(f"SEED_MAX_ROWS is the same cap on both sides ({ce['SEED_MAX_ROWS']:,})", cg["SEED_MAX_ROWS"] == ce["SEED_MAX_ROWS"],
           f"gateway {cg['SEED_MAX_ROWS']:,} / engine {ce['SEED_MAX_ROWS']:,}")

if present(cg, "the gateway", "_CLICKHOUSE_READS") and present(ce, "the engine", "_CH_FEDERATED_ENGINE"):
    reads = set(ce["_CH_FEDERATED_ENGINE"]) | {"clickhouse"}
    verify("_CLICKHOUSE_READS = what the ClickHouse target reads (_CH_FEDERATED_ENGINE and its own server)",
           set(cg["_CLICKHOUSE_READS"]) == reads, f"gateway {sorted(cg['_CLICKHOUSE_READS'])} / engine {sorted(reads)}")

if present(cg, "the gateway", "_FEDERABLE") and present(ce, "the engine", "_DUCK_ATTACH_TYPE"):
    verify("_FEDERABLE = what the federated target attaches (_DUCK_ATTACH_TYPE)", set(cg["_FEDERABLE"]) == set(ce["_DUCK_ATTACH_TYPE"]),
           f"gateway {sorted(cg['_FEDERABLE'])} / engine {sorted(ce['_DUCK_ATTACH_TYPE'])}")

if present(cg, "the gateway", "_NATIVE") and present(ce, "the engine", "_NATIVE_ADAPTER"):
    verify("_NATIVE = the databases with a native adapter (_NATIVE_ADAPTER)", set(cg["_NATIVE"]) == set(ce["_NATIVE_ADAPTER"]),
           f"gateway {sorted(cg['_NATIVE'])} / engine {sorted(ce['_NATIVE_ADAPTER'])}")

allowed = allowed_dialects(route)
if allowed is None:
    verify("ModelSpec.dialect in the engine's route is a Literal", False, "not found")
elif present(cg, "the gateway", "_DIALECT"):
    outside = sorted(set(cg["_DIALECT"].values()) - allowed)
    verify(f"every dialect the gateway sends is one ModelSpec.dialect accepts ({', '.join(sorted(allowed))})", not outside,
           f"the engine would refuse {outside}")

if present(cg, "the gateway", "_NOT_QUOTABLE") and present(ce, "the engine", "_NOT_QUOTABLE"):
    different = []
    for adapter in sorted(set(cg["_NOT_QUOTABLE"]) | set(ce["_NOT_QUOTABLE"])):
        g, e = cg["_NOT_QUOTABLE"].get(adapter), ce["_NOT_QUOTABLE"].get(adapter) or {}
        # a flat list has no role: the gateway refuses it in every name it checks (a source's, and a native
        # output's schema and table), so it stands for every role. Before, it stood for the source role
        # only, and the check passed while the plan offered a native output named o'brien that the engine
        # refused (review2 finding 2). A role only one side lists is a difference too
        roles = g if isinstance(g, dict) else {role: g or () for role in set(e) | {"source"}}
        different += [f"{adapter} {role}: gateway {sorted(set(roles.get(role) or ()))} / engine {sorted(set(e.get(role) or ()))}"
                      for role in sorted(set(roles) | set(e) | {"source"}) if set(roles.get(role) or ()) != set(e.get(role) or ())]
    verify("_NOT_QUOTABLE: the gateway refuses what the engine refuses in each name, role by role (source, relation, schema)",
           not different, "; ".join(different))

# the summary line run_all.py reads
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
