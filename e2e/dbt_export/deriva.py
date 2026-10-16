"""The gateway mirrors what the engine's dbt exporter knows, so that the export plan says before the
download what the engine would refuse (findings #11 and #49, decision 2). This checks, reading the
code (AST only: no import, no stack, no database), that the two sides have not drifted apart:
  · _RICHIESTI (gateway) is the `_require(params, "<key>")` calls of each `op_type` branch of the
    engine's `_compile_op`: a step saved half done is a reason in the plan, not a refused download;
  · SEED_MAX_ROWS, the cap on a seed, is the same number on both sides (seed_too_large);
  · _CLICKHOUSE_LEGGE (gateway) is what the ClickHouse target reads: the engine's
    _CH_MOTORE_FEDERATO and the ClickHouse server itself;
  · _FEDERABLE (gateway) is what the federated target attaches (the engine's _DUCK_ATTACH_TYPE);
  · _NATIVE (gateway) is the engine's _NATIVE_ADAPTER (no_native_adapter);
  · every dialect the gateway sends (_DIALETTO) is one the engine's ModelSpec.dialect accepts;
  · _NON_QUOTABILI: what the gateway refuses in a name is what the engine refuses there
    (name_not_quotable), role by role — a source's schema and table, a model's table, a model's schema —
    for every role either side lists. A flat list (one per adapter) has no role: _quotabile applies it to
    every name it checks, a native output's schema and table too, so it must match every role.
usage: python3 deriva.py [repository root]"""
import ast, os, sys

R = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE = f"{R}/backend/app/engine/dbt_export.py"
ROUTE = f"{R}/backend/app/api/routes/dbt.py"
GATEWAY = f"{R}/gateway/app/services/dbt_export.py"
esiti = []


def verifica(nome: str, ok: bool, dettaglio: str = "") -> None:
    esiti.append(bool(ok))
    print(f"  {'ok' if ok else '✗ '}  {nome}{'' if ok else ': ' + dettaglio}", flush=True)


def costanti(albero: ast.Module) -> dict:
    """The module-level constants written as literals (`X = …` and `X: T = …`)."""
    out = {}
    for n in albero.body:
        bersaglio = n.targets[0] if isinstance(n, ast.Assign) and len(n.targets) == 1 else getattr(n, "target", None)
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(bersaglio, ast.Name) and n.value is not None:
            try:
                out[bersaglio.id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    return out


def tipi_del_ramo(test: ast.expr) -> list[str]:
    """`op_type == "x"` or `op_type in ("x", "y")` → the op types of the branch."""
    if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "op_type":
        destra = test.comparators[0]
        if isinstance(test.ops[0], ast.Eq) and isinstance(destra, ast.Constant):
            return [destra.value]
        if isinstance(test.ops[0], ast.In) and isinstance(destra, (ast.Tuple, ast.List, ast.Set)):
            return [e.value for e in destra.elts if isinstance(e, ast.Constant)]
    return []


def richiesti_dall_engine(albero: ast.Module) -> dict[str, set[str]] | None:
    """The `_require(params, "<key>")` calls of each `if op_type …` branch of `_compile_op` (an
    `elif` chain too: each branch keeps its own calls). None when there is no `_compile_op`."""
    funzione = next((n for n in albero.body if isinstance(n, ast.FunctionDef) and n.name == "_compile_op"), None)
    if funzione is None:
        return None
    out: dict[str, set[str]] = {}
    for nodo in funzione.body:
        while isinstance(nodo, ast.If):
            chiavi = {c.args[1].value for corpo in nodo.body for c in ast.walk(corpo)
                      if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_require"
                      and len(c.args) == 2 and isinstance(c.args[1], ast.Constant)}
            for t in tipi_del_ramo(nodo.test):
                out.setdefault(t, set()).update(chiavi)
            nodo = nodo.orelse[0] if len(nodo.orelse) == 1 else None
    return {t: k for t, k in out.items() if k}


def dialetti_ammessi(albero: ast.Module) -> set[str] | None:
    """The values of `ModelSpec.dialect: Literal[…] | None` in the engine's route."""
    classe = next((n for n in albero.body if isinstance(n, ast.ClassDef) and n.name == "ModelSpec"), None)
    campo = next((n for n in (classe.body if classe else []) if isinstance(n, ast.AnnAssign)
                  and isinstance(n.target, ast.Name) and n.target.id == "dialect"), None)
    if campo is None:
        return None
    for n in ast.walk(campo.annotation):
        if isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "Literal":
            voci = n.slice.elts if isinstance(n.slice, ast.Tuple) else [n.slice]
            return {v.value for v in voci if isinstance(v, ast.Constant)}
    return None


def leggi(percorso: str) -> ast.Module | None:
    try:
        return ast.parse(open(percorso).read())
    except (OSError, SyntaxError) as e:
        verifica(f"{os.path.relpath(percorso, R)} is read", False, str(e))
        return None


engine, route, gateway = leggi(ENGINE), leggi(ROUTE), leggi(GATEWAY)
if None in (engine, route, gateway):
    print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
    sys.exit(1)
ce, cr, cg = costanti(engine), costanti(route), costanti(gateway)


def presenti(lato: dict, dove: str, *nomi: str) -> bool:
    mancano = [n for n in nomi if n not in lato]
    if mancano:
        verifica(f"{', '.join(mancano)} in {dove}", False, "not found as a literal: the check cannot compare it (renamed or moved?)")
    return not mancano


attesi = richiesti_dall_engine(engine)
if attesi is None or not attesi:
    verifica("the engine's _compile_op and its _require calls are found", False, "no _compile_op, or no _require call in it")
elif presenti(cg, "the gateway", "_RICHIESTI"):
    gateway_r = {t: set(k) for t, k in cg["_RICHIESTI"].items()}
    diversi = sorted(t for t in set(gateway_r) | set(attesi) if gateway_r.get(t, set()) != attesi.get(t, set()))
    verifica(f"_RICHIESTI = the _require calls of the engine's _compile_op ({len(attesi)} step types)", not diversi,
             "; ".join(f"{t}: gateway {sorted(gateway_r.get(t, ()))} / engine {sorted(attesi.get(t, ()))}" for t in diversi))

if presenti(cg, "the gateway", "SEED_MAX_ROWS") and presenti(ce, "the engine", "SEED_MAX_ROWS"):
    verifica(f"SEED_MAX_ROWS is the same cap on both sides ({ce['SEED_MAX_ROWS']:,})", cg["SEED_MAX_ROWS"] == ce["SEED_MAX_ROWS"],
             f"gateway {cg['SEED_MAX_ROWS']:,} / engine {ce['SEED_MAX_ROWS']:,}")

if presenti(cg, "the gateway", "_CLICKHOUSE_LEGGE") and presenti(ce, "the engine", "_CH_MOTORE_FEDERATO"):
    legge = set(ce["_CH_MOTORE_FEDERATO"]) | {"clickhouse"}
    verifica("_CLICKHOUSE_LEGGE = what the ClickHouse target reads (_CH_MOTORE_FEDERATO and its own server)",
             set(cg["_CLICKHOUSE_LEGGE"]) == legge, f"gateway {sorted(cg['_CLICKHOUSE_LEGGE'])} / engine {sorted(legge)}")

if presenti(cg, "the gateway", "_FEDERABLE") and presenti(ce, "the engine", "_DUCK_ATTACH_TYPE"):
    verifica("_FEDERABLE = what the federated target attaches (_DUCK_ATTACH_TYPE)", set(cg["_FEDERABLE"]) == set(ce["_DUCK_ATTACH_TYPE"]),
             f"gateway {sorted(cg['_FEDERABLE'])} / engine {sorted(ce['_DUCK_ATTACH_TYPE'])}")

if presenti(cg, "the gateway", "_NATIVE") and presenti(ce, "the engine", "_NATIVE_ADAPTER"):
    verifica("_NATIVE = the databases with a native adapter (_NATIVE_ADAPTER)", set(cg["_NATIVE"]) == set(ce["_NATIVE_ADAPTER"]),
             f"gateway {sorted(cg['_NATIVE'])} / engine {sorted(ce['_NATIVE_ADAPTER'])}")

ammessi = dialetti_ammessi(route)
if ammessi is None:
    verifica("ModelSpec.dialect in the engine's route is a Literal", False, "not found")
elif presenti(cg, "the gateway", "_DIALETTO"):
    fuori = sorted(set(cg["_DIALETTO"].values()) - ammessi)
    verifica(f"every dialect the gateway sends is one ModelSpec.dialect accepts ({', '.join(sorted(ammessi))})", not fuori,
             f"the engine would refuse {fuori}")

if presenti(cg, "the gateway", "_NON_QUOTABILI") and presenti(ce, "the engine", "_NON_QUOTABILI"):
    diversi = []
    for adapter in sorted(set(cg["_NON_QUOTABILI"]) | set(ce["_NON_QUOTABILI"])):
        g, e = cg["_NON_QUOTABILI"].get(adapter), ce["_NON_QUOTABILI"].get(adapter) or {}
        # a flat list has no role: the gateway refuses it in every name it checks (a source's, and a native
        # output's schema and table), so it stands for every role. Before, it stood for the source role
        # only, and the check passed while the plan offered a native output named o'brien that the engine
        # refused (review2 finding 2). A role only one side lists is a difference too
        ruoli = g if isinstance(g, dict) else {ruolo: g or () for ruolo in set(e) | {"source"}}
        diversi += [f"{adapter} {ruolo}: gateway {sorted(set(ruoli.get(ruolo) or ()))} / engine {sorted(set(e.get(ruolo) or ()))}"
                    for ruolo in sorted(set(ruoli) | set(e) | {"source"}) if set(ruoli.get(ruolo) or ()) != set(e.get(ruolo) or ())]
    verifica("_NON_QUOTABILI: the gateway refuses what the engine refuses in each name, role by role (source, relation, schema)",
             not diversi, "; ".join(diversi))

# the summary line esegui.py reads
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
