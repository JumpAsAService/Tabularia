"""Ogni codice d'errore dell'export dbt sollevato da engine e gateway ha la sua frase
nel catalogo del gateway (in cinque lingue); elenca i rifiuti senza codice (quelli
restano «internal»: devono essere invarianti interne, non cose che un utente incontra).
uso: python3 codici.py"""
import ast, importlib.util, sys

from ambiente import REPO as R
FILE = [f"{R}/backend/app/engine/dbt_export.py", f"{R}/backend/app/api/routes/dbt.py", f"{R}/gateway/app/services/dbt_export.py"]

spec = importlib.util.spec_from_file_location("lingua", f"{R}/gateway/app/core/lingua.py")
lingua = importlib.util.module_from_spec(spec); spec.loader.exec_module(lingua)
sys.modules["app.core.lingua"] = lingua
spec = importlib.util.spec_from_file_location("messaggi", f"{R}/gateway/app/services/messaggi_dbt.py")
messaggi = importlib.util.module_from_spec(spec); spec.loader.exec_module(messaggi)

usati, senza = {}, []
for f in FILE:
    for nodo in ast.walk(ast.parse(open(f).read())):
        if isinstance(nodo, ast.Call) and getattr(nodo.func, "id", None) == "DbtExportError":
            codice = nodo.args[1].value if len(nodo.args) > 1 and isinstance(nodo.args[1], ast.Constant) else None
            parametri = {k.arg for k in nodo.keywords}
            if codice is None:
                senza.append(f"{f.rsplit('/', 3)[-2]}/{f.rsplit('/', 1)[-1]}:{nodo.lineno}")
            else:
                usati.setdefault(codice, []).append((f, nodo.lineno, parametri))

esiti = []
for codice, dove in sorted(usati.items()):
    modello = messaggi.MESSAGGI.get(codice, {}).get("en")
    if modello is None:
        esiti.append(False); print(f"  ✗  {codice}: manca nel catalogo ({dove[0][0].rsplit('/', 1)[-1]}:{dove[0][1]})"); continue
    import string
    attesi = {c for _, c, _, _ in string.Formatter().parse(modello) if c}
    for f, riga, parametri in dove:
        ok = attesi <= parametri
        esiti.append(ok)
        if not ok:
            print(f"  ✗  {codice} in {f.rsplit('/', 1)[-1]}:{riga}: mancano i parametri {sorted(attesi - parametri)}")
print(f"  {len(usati)} codici usati, tutti nel catalogo con i loro parametri" if all(esiti) else "")
print(f"  rifiuti senza codice (restano «internal»): {', '.join(senza)}")
inutili = sorted(set(messaggi.MESSAGGI) - set(usati) - {"internal"})
print(f"  nel catalogo e mai usati: {inutili or 'nessuno'}")
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
