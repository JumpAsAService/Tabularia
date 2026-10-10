"""Prova DAL VIVO delle descrizioni AI nell'export dbt (pezzo 4), sullo stack di
sviluppo col provider vero: export con l'opzione, controllo dei testi, secondo
export identico (memoria per versione, nessuna nuova spesa), dbt parse + docs generate.
uso: python3 ai_descrizioni.py (serve un modello AI abilitato; è una chiamata a pagamento al provider)"""
import hashlib, io, os, re, subprocess, sys, zipfile
from ambiente import DOVE, IMMAGINE
from comune import api, dbt, entra, ok, esiti, password_delle_connessioni

FLUSSO = "Margin by Category and Month"
entra()
flusso = next(f for f in api("GET", "/flows")[1] if f["name"] == FLUSSO)
piano = api("GET", f"/flows/{flusso['id']}/export/dbt/plan")[1]
ok("il piano dice che l'AI c'è, col modello", piano.get("ai", {}).get("available"), piano.get("ai"))
print("    modello:", piano.get("ai"))
spesa_prima = api("GET", "/ai/usage")[1] if api("GET", "/ai/usage")[0] == 200 else None


def scarica():
    st, dati = api("POST", f"/flows/{flusso['id']}/export/dbt", {"target": "duckdb", "ai_descriptions": True}, grezzo=True)
    return st, dati


st, primo = scarica()
if not ok("l'export con le descrizioni AI risponde con uno zip", st == 200 and primo[:2] == b"PK", (st, primo[:400] if isinstance(primo, (bytes, str)) else primo)):
    print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate"); sys.exit(1)
files = {n: zipfile.ZipFile(io.BytesIO(primo)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(primo)).namelist()}
readme = files["README.md"]
ai = [l.strip() for f in ("models/schema.yml", "models/sources.yml") for l in files.get(f, "").splitlines() if "(AI)" in l]
ok("il README ha «What the flow does» e l'avvertenza sulle descrizioni (AI)", "## What the flow does" in readme and "(AI)" in readme, readme[:800])
ok(f"ci sono descrizioni (AI) nelle colonne ({len(ai)})", len(ai) >= 3, ai[:5])
ok("nessun delimitatore Jinja nei testi dell'AI", not any(re.search(r"\{\{|\{%|\{#", l) for l in ai))
print("    " + readme[readme.index("## What the flow does"):][:700].replace("\n", "\n    ") if "## What the flow does" in readme else "")
for l in ai[:6]:
    print("    " + l[:220])
st, secondo = scarica()
ok("il secondo export della stessa versione è lo stesso zip (memoria per versione)", st == 200 and hashlib.sha256(primo).hexdigest() == hashlib.sha256(secondo).hexdigest())
cartella = f"{DOVE}/ai_descrizioni"
subprocess.run(["docker", "run", "--rm", "-v", f"{DOVE}:/x", IMMAGINE, "sh", "-c", "rm -rf /x/ai_descrizioni"], capture_output=True)
os.makedirs(cartella)
zipfile.ZipFile(io.BytesIO(primo)).extractall(cartella)
variabili = password_delle_connessioni()
for comando in (["parse"], ["docs", "generate"]):
    rc, out = dbt(cartella, *comando, variabili=variabili)
    ok(f"dbt {' '.join(comando)} sul progetto con le descrizioni AI", rc == 0, out[-800:])
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
