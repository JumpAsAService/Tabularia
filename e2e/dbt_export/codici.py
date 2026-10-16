"""Ogni codice d'errore dell'export dbt sollevato da engine e gateway ha la sua frase
nel catalogo del gateway (in cinque lingue); elenca i rifiuti senza codice (quelli
restano «internal»: devono essere invarianti interne, non cose che un utente incontra).
Also the reasons of `ai_translation_invalid` (MOTIVI_AI), and no catalogue entry that
nothing raises (a dead entry, or a file that raises it missing from FILE).
uso: python3 codici.py"""
import ast, importlib.util, string, sys

from ambiente import REPO as R
# every file that raises DbtExportError (gateway/app/routes/flows.py only catches it)
FILE = [f"{R}/backend/app/engine/dbt_export.py", f"{R}/backend/app/api/routes/dbt.py",
        f"{R}/gateway/app/services/dbt_export.py", f"{R}/gateway/app/services/dbt_ai.py"]
LINGUE = ("en", "it", "de", "es", "fr")

spec = importlib.util.spec_from_file_location("lingua", f"{R}/gateway/app/core/lingua.py")
lingua = importlib.util.module_from_spec(spec); spec.loader.exec_module(lingua)
sys.modules["app.core.lingua"] = lingua
spec = importlib.util.spec_from_file_location("messaggi", f"{R}/gateway/app/services/messaggi_dbt.py")
messaggi = importlib.util.module_from_spec(spec); spec.loader.exec_module(messaggi)


def segnaposti(modello: str) -> set[str]:
    return {c for _, c, _, _ in string.Formatter().parse(modello) if c}


def breve(f: str) -> str:
    return f.rsplit("/", 1)[-1]


usati, senza, motivi = {}, [], {}
for f in FILE:
    albero = ast.parse(open(f).read())
    # verifica_proposta's own no(reason, …): another function may have a helper of that name
    proposta = {id(n) for d in ast.walk(albero) if isinstance(d, ast.FunctionDef) and d.name == "verifica_proposta" for n in ast.walk(d)}
    for nodo in ast.walk(albero):
        if not isinstance(nodo, ast.Call):
            continue
        nome = getattr(nodo.func, "id", None)
        parametri = {k.arg for k in nodo.keywords if k.arg}
        if nome == "DbtExportError":
            codice = nodo.args[1].value if len(nodo.args) > 1 and isinstance(nodo.args[1], ast.Constant) else None
            if codice is None:
                senza.append(f"{f.rsplit('/', 3)[-2]}/{breve(f)}:{nodo.lineno}")
            else:
                usati.setdefault(codice, []).append((f, nodo.lineno, parametri))
            motivo = next((k.value.value for k in nodo.keywords if k.arg == "reason" and isinstance(k.value, ast.Constant)), None)
            if codice == "ai_translation_invalid" and motivo:
                motivi.setdefault(motivo, []).append((f, nodo.lineno, parametri))
        elif nome == "no" and id(nodo) in proposta and nodo.args and isinstance(nodo.args[0], ast.Constant):
            # verifica_proposta's no(reason, text, **params) → ai_translation_invalid {model, reason, **params}
            motivi.setdefault(nodo.args[0].value, []).append((f, nodo.lineno, parametri | {"model", "reason"}))

esiti = []


def verifica(catalogo: dict, chiave: str, dove: list, cosa: str, facoltativi: set = frozenset()):
    voce = catalogo.get(chiave)
    if voce is None:
        esiti.append(False); print(f"  ✗  {cosa} {chiave}: manca nel catalogo ({breve(dove[0][0])}:{dove[0][1]})"); return
    mancano = [l for l in LINGUE if not voce.get(l)]
    esiti.append(not mancano)
    if mancano:
        print(f"  ✗  {cosa} {chiave}: no sentence in {mancano}")
    for f, riga, parametri in dove:
        for l in LINGUE:
            attesi = segnaposti(voce.get(l) or "") - facoltativi
            ok = attesi <= parametri
            esiti.append(ok)
            if not ok:
                print(f"  ✗  {cosa} {chiave} in {breve(f)}:{riga}: mancano i parametri {sorted(attesi - parametri)} ({l})")


for codice, dove in sorted(usati.items()):
    # «internal» gets its {detail} from the English text when the raise has none (messaggi_dbt.traduci)
    verifica(messaggi.MESSAGGI, codice, dove, "codice", {"detail"} if codice == "internal" else set())
for motivo, dove in sorted(motivi.items()):
    verifica(messaggi.MOTIVI_AI, motivo, dove, "motivo AI")
if all(esiti):
    print(f"  {len(usati)} codici usati e {len(motivi)} motivi AI, tutti nel catalogo con i loro parametri, in {len(LINGUE)} lingue")
print(f"  rifiuti senza codice (restano «internal»): {', '.join(senza)}")
inutili = sorted(set(messaggi.MESSAGGI) - set(usati) - {"internal"})
motivi_inutili = sorted(set(messaggi.MOTIVI_AI) - set(motivi))
esiti.append(not inutili and not motivi_inutili)
print(f"  {'✗  ' if inutili else ''}nel catalogo e mai usati: {inutili or 'nessuno'}")
print(f"  {'✗  ' if motivi_inutili else ''}motivi AI mai usati: {motivi_inutili or 'nessuno'}")
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
