"""Every error code of the dbt export raised by engine and gateway has its sentence in
the gateway's catalogue (in five languages); lists the refusals without a code (those
stay «internal»: they must be internal invariants, not things a user runs into).
Also the reasons of `ai_translation_invalid` (AI_REASONS), and no catalogue entry that
nothing raises (a dead entry, or a file that raises it missing from FILES).
usage: python3 codes.py"""
import ast, importlib.util, string, sys

from environment import REPO as R
# every file that raises DbtExportError (gateway/app/routes/flows.py only catches it)
FILES = [f"{R}/backend/app/engine/dbt_export.py", f"{R}/backend/app/api/routes/dbt.py",
         f"{R}/gateway/app/services/dbt_export.py", f"{R}/gateway/app/services/dbt_ai.py"]
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

outcomes = []


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
    verify(messages.MESSAGES, code, where, "code", {"detail"} if code == "internal" else set())
for reason, where in sorted(reasons.items()):
    verify(messages.AI_REASONS, reason, where, "AI reason")
if all(outcomes):
    print(f"  {len(used)} codes used and {len(reasons)} AI reasons, all in the catalogue with their parameters, in {len(LANGUAGES)} languages")
print(f"  refusals without a code (they stay «internal»): {', '.join(without)}")
unused = sorted(set(messages.MESSAGES) - set(used) - {"internal"})
unused_reasons = sorted(set(messages.AI_REASONS) - set(reasons))
outcomes.append(not unused and not unused_reasons)
print(f"  {'✗  ' if unused else ''}in the catalogue and never used: {unused or 'none'}")
print(f"  {'✗  ' if unused_reasons else ''}AI reasons never used: {unused_reasons or 'none'}")
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
