"""Every error code of the dbt export raised by engine and gateway has its sentence in
the gateway's catalogue (in five languages); lists the refusals without a code (those
stay «internal»: they must be internal invariants, not things a user runs into).
Also the reasons of `ai_translation_invalid` (AI_REASONS), and no catalogue entry that
nothing raises (a dead entry, or a file that raises it missing from FILES).
The gateway that answers is the Go one (gateway-go/internal/dbtexport, its catalogue in
messages_gen.go); an export with the AI options goes on to the AI service, the Python
gateway (gateway/app/services/dbt_export.py and dbt_ai.py, its catalogue dbt_messages.py):
both raise, both catalogues must say the same.
usage: python3 codes.py"""
import ast, importlib.util, string, sys

from environment import REPO as R
import golit
# every file that raises DbtExportError (gateway/app/routes/flows.py only catches it)
FILES = [f"{R}/backend/app/engine/dbt_export.py", f"{R}/backend/app/api/routes/dbt.py",
         f"{R}/gateway/app/services/dbt_export.py", f"{R}/gateway/app/services/dbt_ai.py"]
# the Go gateway: refuse(message, code, …) and Error{Code: …} (internal/api/flows_dbt.go answers the engine's)
GO_FILES = [f"{R}/gateway-go/internal/dbtexport/export.go", f"{R}/gateway-go/internal/dbtexport/payload.go",
            f"{R}/gateway-go/internal/api/flows_dbt.go"]
GO_CATALOGUE = f"{R}/gateway-go/internal/dbtexport/messages_gen.go"
LANGUAGES = ("en", "it", "de", "es", "fr")

spec = importlib.util.spec_from_file_location("language", f"{R}/gateway/app/core/language.py")
language = importlib.util.module_from_spec(spec); spec.loader.exec_module(language)
sys.modules["app.core.language"] = language
spec = importlib.util.spec_from_file_location("messages", f"{R}/gateway/app/services/dbt_messages.py")
messages = importlib.util.module_from_spec(spec); spec.loader.exec_module(messages)


def placeholders(template: str) -> set[str]:
    return {c for _, c, _, _ in string.Formatter().parse(template) if c}


def short(f: str) -> str:
    return f.rsplit("/", 1)[-1]


used, without, reasons = {}, [], {}
for f in FILES:
    tree = ast.parse(open(f).read())
    # check_proposal's own no(reason, …): another function may have a helper of that name
    proposal = {id(n) for d in ast.walk(tree) if isinstance(d, ast.FunctionDef) and d.name == "check_proposal" for n in ast.walk(d)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        params = {k.arg for k in node.keywords if k.arg}
        if name == "DbtExportError":
            code = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
            if code is None:
                without.append(f"{f.rsplit('/', 3)[-2]}/{short(f)}:{node.lineno}")
            else:
                used.setdefault(code, []).append((f, node.lineno, params))
            reason = next((k.value.value for k in node.keywords if k.arg == "reason" and isinstance(k.value, ast.Constant)), None)
            if code == "ai_translation_invalid" and reason:
                reasons.setdefault(reason, []).append((f, node.lineno, params))
        elif name == "no" and id(node) in proposal and node.args and isinstance(node.args[0], ast.Constant):
            # check_proposal's no(reason, text, **params) → ai_translation_invalid {model, reason, **params}
            reasons.setdefault(node.args[0].value, []).append((f, node.lineno, params | {"model", "reason"}))

for f in GO_FILES:
    for code, line, params in golit.raises(open(f).read()):
        if code is None:
            without.append(f"{f.rsplit('/', 2)[-2]}/{short(f)}:{line}")
        else:
            used.setdefault(code, []).append((f, line, params))

outcomes = []
# the Go gateway answers with its own copy of the catalogue (generated from dbt_messages.py)
go_source = open(GO_CATALOGUE).read()
go_messages, go_reasons = golit.value(go_source, "messages"), golit.value(go_source, "aiReasons")
for name, go, py in (("MESSAGES", go_messages, messages.MESSAGES), ("AI_REASONS", go_reasons, messages.AI_REASONS)):
    different = sorted(k for k in set(go) | set(py) if go.get(k) != py.get(k))
    outcomes.append(not different)
    print(f"  {'✗  ' if different else ''}{name}: the Go gateway's catalogue (messages_gen.go) and the AI service's (dbt_messages.py) "
          + (f"differ on {different}" if different else f"say the same ({len(go)} entries)"))


def verify(catalogue: dict, key: str, where: list, what: str, optional: set = frozenset()):
    item = catalogue.get(key)
    if item is None:
        outcomes.append(False); print(f"  ✗  {what} {key}: missing from the catalogue ({short(where[0][0])}:{where[0][1]})"); return
    missing = [l for l in LANGUAGES if not item.get(l)]
    outcomes.append(not missing)
    if missing:
        print(f"  ✗  {what} {key}: no sentence in {missing}")
    for f, line, params in where:
        for l in LANGUAGES:
            expected = placeholders(item.get(l) or "") - optional
            ok = expected <= params
            outcomes.append(ok)
            if not ok:
                print(f"  ✗  {what} {key} in {short(f)}:{line}: missing parameters {sorted(expected - params)} ({l})")


for code, where in sorted(used.items()):
    # «internal» gets its {detail} from the English text when the raise has none (dbt_messages.translate)
    verify(go_messages, code, where, "code", {"detail"} if code == "internal" else set())
for reason, where in sorted(reasons.items()):
    verify(go_reasons, reason, where, "AI reason")
if all(outcomes):
    print(f"  {len(used)} codes used and {len(reasons)} AI reasons, all in the catalogue with their parameters, in {len(LANGUAGES)} languages")
print(f"  refusals without a code (they stay «internal»): {', '.join(without)}")
unused = sorted(set(go_messages) - set(used) - {"internal"})
unused_reasons = sorted(set(go_reasons) - set(reasons))
outcomes.append(not unused and not unused_reasons)
print(f"  {'✗  ' if unused else ''}in the catalogue and never used: {unused or 'none'}")
print(f"  {'✗  ' if unused_reasons else ''}AI reasons never used: {unused_reasons or 'none'}")
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
