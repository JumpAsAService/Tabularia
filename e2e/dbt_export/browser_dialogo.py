"""Prova nel browser del dialogo dell'export dbt (pezzo 3): dalla pagina Flows,
Export → dbt apre il dialogo con le opzioni; si sceglie target, cartella per un
progetto esistente, prefisso, schema, livelli, nome di una sorgente, una
materializzazione, niente test né email; lo zip scaricato ha esattamente quelle
scelte; il dialogo le ricorda; un rifiuto dell'engine si legge nel dialogo; Esc chiude.
Firefox headless via Marionette contro lo stack di scarto sulla 8088.
Serve Firefox (headless, con Marionette) sulla macchina che lancia la prova.
uso: python3 browser_dialogo.py"""
import base64, io, json, os, re, subprocess, sys, time, urllib.error, urllib.request, zipfile

from ambiente import DOVE, UI, env
from marionette import Firefox, ferma

BASE = UI
PROFILO = os.path.join(DOVE, "firefox-dbt-dialogo")
FOTO = DOVE
FLUSSO = "Margin by Category and Month"      # sorgenti su Postgres: tutti e tre i target
FLUSSO_CRM = "Agent Leaderboard"             # legge il ClickHouse del CRM, che NON è quello del motore
esiti = []


def ok(nome, cond, dettaglio=""):
    esiti.append(bool(cond))
    print(("  ok " if cond else "  ✗  ") + nome + ("" if cond else f"  → {str(dettaglio)[:600]}"), flush=True)
    return bool(cond)




def api(metodo, path, corpo=None, token=None):
    req = urllib.request.Request(BASE + "/api" + path, method=metodo, data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        testo = r.read()
        return json.loads(testo) if testo else None


admin = api("POST", "/auth/login", {"email": env("AUTH__ADMIN_EMAIL"), "password": env("AUTH__ADMIN_PASSWORD")})["access_token"]
flussi = {f["name"]: f for f in api("GET", "/flows", token=admin)}
flusso, crm = flussi[FLUSSO], flussi[FLUSSO_CRM]
piano = api("GET", f"/flows/{flusso['id']}/export/dbt/plan", token=admin)


def apri_export(nome):
    ff.js(f"""
      const azioni = Array.from(document.querySelectorAll('.flow-actions')).find(a => a.parentElement.innerText.includes({json.dumps(nome)}));
      azioni.querySelector('button svg.lucide-download').closest('button').click(); return 1""")
    ff.aspetta("!!document.querySelector('.export-overlay .export-card')", 10)
    ff.js("Array.from(document.querySelectorAll('.export-overlay .export-opt')).find(b => b.querySelector('strong').innerText.trim() === 'dbt').click(); return 1")
    return ff.aspetta("!!document.querySelector('.dx-card') && !document.querySelector('.export-overlay') && document.querySelectorAll('.dx-card input[name=dx-target]').length === 3", 30)


def scrivi(selettore, valore):
    """Un valore in un campo, come lo scrive una persona (Vue ascolta `input`)."""
    return ff.js(f"""
      const el = document.querySelector({json.dumps(selettore)}); if (!el) return false;
      el.value = {json.dumps(valore)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); return true""")


os.makedirs(PROFILO, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILO, "--window-size", "1500,1000", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.invia("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1000})
    ff.vai(f"{BASE}/login")
    ff.js("localStorage.clear(); localStorage.setItem('tabularia-theme', 'dark'); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.vai(f"{BASE}/auth/callback#token={admin}")
    ff.aspetta("!location.pathname.startsWith('/auth/callback')", 20)
    ff.vai(f"{BASE}/flows")
    ok("la pagina Flows elenca i flussi", ff.aspetta("document.querySelectorAll('.flow-actions').length > 3", 30))
    # lo zip che la pagina manda al download, catturato in base64 senza scaricare davvero
    ff.js("""
      window.__scaricati = []; window.__zip = null;
      const crea = URL.createObjectURL.bind(URL);
      URL.createObjectURL = (b) => { const r = new FileReader(); r.onload = () => { window.__zip = r.result.split(',')[1] }; r.readAsDataURL(b); return crea(b) };
      const click = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () { if (this.download) { window.__scaricati.push(this.download); return } return click.apply(this, arguments) };
      return 1""")

    ok("Export → dbt apre il dialogo delle opzioni, coi tre target", apri_export(FLUSSO))
    ok("è un dialogo accessibile, col fuoco dentro", ff.js("const c = document.querySelector('.dx-card'); return c.getAttribute('role') === 'dialog' && c.getAttribute('aria-modal') === 'true' && c.contains(document.activeElement)"))
    scelto = ff.js("return document.querySelector('.dx-card input[name=dx-target]:checked')?.value")
    ok("il target di partenza è ClickHouse (il primo che funziona)", scelto == "clickhouse", scelto)
    sorgenti = ff.js("return document.querySelectorAll('.dx-source').length")
    uscite = ff.js("return Array.from(document.querySelectorAll('.dx-output select')).map(s => s.value)")
    attese = next(t for t in piano["targets"] if t["id"] == "clickhouse")
    ok("il dialogo mostra le sorgenti e le uscite del piano", sorgenti == len(attese["sources"]) >= 1 and uscite == [o["materialized"] for o in piano["outputs"]], (sorgenti, uscite))
    time.sleep(0.4)
    ff.foto(f"{FOTO}/dbt-dialogo.png")

    # le scelte
    ff.js("document.querySelector('.dx-card input[name=dx-package][value=folder]').click(); return 1")
    scrivi(".dx-card .dx-row .dx-field:nth-child(1) input", "margini")
    scrivi(".dx-card .dx-row .dx-field:nth-child(3) input", "analisi")
    ff.js("Array.from(document.querySelectorAll('.dx-card .dx-check')).find(l => l.innerText.includes('staging')).querySelector('input').click(); return 1")
    # un nome sbagliato si vede subito e il download resta spento
    scrivi(".dx-card .dx-row .dx-field:nth-child(2) input", "Tab-")
    ok("un prefisso non valido si segnala e spegne il download",
       ff.aspetta("!!document.querySelector('.dx-bad') && document.querySelector('.dx-actions .primary').disabled", 5))
    scrivi(".dx-card .dx-row .dx-field:nth-child(2) input", "tab_")
    scrivi(".dx-card .dx-source input[type=text]", "erp")
    ff.js("""
      const s = document.querySelector('.dx-card .dx-output select');
      s.value = 'view'; s.dispatchEvent(new Event('change', {bubbles: true}));
      for (const l of document.querySelectorAll('.dx-card .dx-check')) {
        if (/data contract|Who built/.test(l.innerText)) l.querySelector('input').click();
      }
      return 1""")
    ok("con valori validi il download si riaccende", ff.aspetta("!document.querySelector('.dx-bad') && !document.querySelector('.dx-actions .primary').disabled", 5))
    ff.js("document.querySelector('.dx-actions .primary').click(); return 1")
    scaricato = ff.aspetta("window.__scaricati.length === 1 && window.__zip !== null && !document.querySelector('.dx-card')", 120)
    nome = ff.js("return window.__scaricati[0]")
    ok("scarica lo zip e chiude il dialogo", scaricato and nome == "Margin_by_Category_and_Month_dbt_clickhouse_folder.zip",
       ff.js("return [window.__scaricati, document.querySelector('.dx-error')?.innerText]"))

    files = {}
    if scaricato:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(ff.js("return window.__zip")))) as z:
            files = {n: z.read(n).decode() for n in z.namelist()}
    nomi = sorted(files)
    ok("una cartella per un progetto esistente: niente profilo né dbt_project.yml, c'è INTEGRATION.md",
       "INTEGRATION.md" in files and "profiles.yml" not in files and "dbt_project.yml" not in files, nomi)
    ok("i modelli stanno in models/margini/, per livello, col prefisso tab_",
       any(re.fullmatch(r"models/margini/marts/tab_\w+\.sql", n) for n in nomi) and all(n.startswith(("models/margini/", "macros/margini/", "seeds/margini/", "tests/margini/", "INTEGRATION")) for n in nomi), nomi)
    sources = files.get("models/margini/sources.yml", "")
    ok("la sorgente ha il nome scelto", "  - name: erp\n" in sources and "{{ source('erp', " in "".join(v for k, v in files.items() if k.endswith(".sql")), sources[:300])
    schema = files.get("models/margini/schema.yml", "")
    ok("niente test del contratto né email nei metadati", "data_tests" not in schema and "owner:" not in schema and "exported_by" not in schema and "flow_id:" in schema, schema[:400])
    viste = [n for n, v in files.items() if n.endswith(".sql") and "materialized='view'" in v]
    ok("l'uscita scelta è una view", len(viste) == 1, viste)
    guida = files.get("INTEGRATION.md", "")
    ok("INTEGRATION.md dice cosa copiare, join_use_nulls, il comando dei database federati e come lanciarla",
       "`models/margini/`" in guida and "join_use_nulls: 1" in guida and "create_tabularia_sources" in guida and "dbt build -s +tag:" in guida, guida[:600])

    # le scelte restano, per questo flusso
    ok("riaperto, il dialogo ricorda le scelte", apri_export(FLUSSO) and ff.aspetta(
        "document.querySelector('.dx-card input[name=dx-package][value=folder]').checked && document.querySelector('.dx-card .dx-row .dx-field:nth-child(2) input').value === 'tab_'"
        " && document.querySelector('.dx-card .dx-source input[type=text]').value === 'erp'", 10))
    ff.js("document.querySelector('.dx-card').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return 1")
    ok("Esc chiude il dialogo", ff.aspetta("!document.querySelector('.dx-card')", 5))

    # un rifiuto che sa solo l'engine (un ClickHouse che non è il suo) si legge nel dialogo
    ok("il dialogo si apre anche per un flusso sul CRM", apri_export(FLUSSO_CRM))
    ff.js("document.querySelector('.dx-card input[name=dx-target][value=clickhouse]').click(); document.querySelector('.dx-actions .primary').click(); return 1")
    ok("il motivo del rifiuto è nel dialogo, in chiaro (non «422»)", ff.aspetta("!!document.querySelector('.dx-error')", 60) and "ClickHouse" in (ff.js("return document.querySelector('.dx-error').innerText") or "")
       and "422" not in ff.js("return document.querySelector('.dx-error').innerText"), ff.js("return document.querySelector('.dx-error')?.innerText"))
    time.sleep(0.3)
    ff.foto(f"{FOTO}/dbt-dialogo-rifiuto.png")
    ok("e il dialogo resta aperto per scegliere un altro target", ff.js("return !!document.querySelector('.dx-card')"))

    # l'AI (pezzo 4): le opzioni compaiono solo se c'è un modello abilitato; con le descrizioni il progetto le porta
    if piano.get("ai", {}).get("available"):
        ok("con l'AI disponibile il dialogo mostra le due opzioni AI", apri_export(FLUSSO) and ff.js(
            "return Array.from(document.querySelectorAll('.dx-card .dx-check')).filter(l => /AI/.test(l.innerText)).length") == 2)
        ff.js("Array.from(document.querySelectorAll('.dx-card .dx-check')).find(l => /Descriptions written by AI/.test(l.innerText)).querySelector('input').click(); return 1")
        nota = ff.js("return Array.from(document.querySelectorAll('.dx-card .dx-hint')).map(e => e.innerText).find(t => /sample values/.test(t)) || ''")
        ok("spuntata, una nota dice il modello e cosa vede l'AI", piano["ai"]["model"] in nota and "3 sample values" in nota, nota)
        ff.js("window.__scaricati = []; window.__zip = null; document.querySelector('.dx-actions .primary').click(); return 1")
        scaricato = ff.aspetta("window.__scaricati.length === 1 && window.__zip !== null && !document.querySelector('.dx-card')", 300)
        ok("lo zip con le descrizioni dell'AI si scarica", scaricato, ff.js("return document.querySelector('.dx-error')?.innerText"))
        if scaricato:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(ff.js("return window.__zip")))) as z:
                tutti = {n: z.read(n).decode() for n in z.namelist()}
            guida = tutti.get("INTEGRATION.md") or tutti.get("README.md") or ""
            ok("c'è il paragrafo «What the flow does» e le descrizioni (AI)", "## What the flow does" in guida
               and any("(AI)" in v for k, v in tutti.items() if k.endswith(".yml")), guida[:400])
    else:
        print("  (AI non configurata sullo stack: opzioni AI non provate)")

    # in italiano: i motivi del piano e il rifiuto dell'engine arrivano in italiano
    ff.js("document.cookie = 'tabularia-locale=it; path=/; max-age=86400; samesite=lax'; return 1")
    ff.vai(f"{BASE}/flows")
    ff.aspetta("document.querySelectorAll('.flow-actions').length > 3", 30)
    ff.js("""
      window.__scaricati = []; window.__zip = null;
      const click = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () { if (this.download) { window.__scaricati.push(this.download); return } return click.apply(this, arguments) };
      return 1""")
    ok("in italiano il dialogo si apre", apri_export(FLUSSO_CRM))
    motivi = ff.js("return Array.from(document.querySelectorAll('.dx-why')).map(e => e.innerText)")
    ok("i motivi dei target non disponibili sono in italiano", motivi and all(m.startswith("Non disponibile:") for m in motivi)
       and any("DuckDB non la collega" in m for m in motivi), motivi)
    ff.js("document.querySelector('.dx-card input[name=dx-target][value=clickhouse]').click(); document.querySelector('.dx-actions .primary').click(); return 1")
    ok("il rifiuto dell'engine è in italiano", ff.aspetta("!!document.querySelector('.dx-error')", 60)
       and "è un ALTRO ClickHouse" in (ff.js("return document.querySelector('.dx-error').innerText") or ""), ff.js("return document.querySelector('.dx-error')?.innerText"))
    time.sleep(0.3)
    ff.foto(f"{FOTO}/dbt-dialogo-it.png")
finally:
    ferma(proc)

# un utente comune: niente piano né download
u = api("POST", "/users", {"email": "zzbrowser-dbt@example.com", "password": "Lettore-di-prova-9", "full_name": "zz"}, token=admin)
try:
    api("POST", f"/projects/{flusso['project_id']}/permissions", {"capability": "view", "user_id": u["id"]}, token=admin)
    lettore = api("POST", "/auth/login", {"email": "zzbrowser-dbt@example.com", "password": "Lettore-di-prova-9"})["access_token"]
    stati = []
    for metodo, path, corpo in (("GET", f"/flows/{flusso['id']}/export/dbt/plan", None), ("POST", f"/flows/{flusso['id']}/export/dbt", {"target": "duckdb"})):
        try:
            api(metodo, path, corpo, token=lettore); stati.append(200)
        except urllib.error.HTTPError as e:
            stati.append(e.code)
    ok("a un utente comune il piano e il download con opzioni rispondono 403", stati == [403, 403], stati)
finally:
    api("DELETE", f"/users/{u['id']}", token=admin)
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
