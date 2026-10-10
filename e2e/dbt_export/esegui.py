"""Gli oracoli dell'export dbt contro uno stack in esecuzione (vedi README.md).

uso: python3 esegui.py [veloce|completo|browser|ai|tutto] [nome …]
  veloce    codici d'errore, giro dbt, sicurezza dell'export            (~5 min)
  completo  veloce + matrice, ciclo di vita, cartella, flussi d'esempio,
            determinismo                                                 (~40 min, default)
  browser   il dialogo dell'export in Firefox
  ai        descrizioni e traduzione con l'AI vera (chiamate a pagamento)
  tutto     completo + browser + ai
  nome …    solo quegli oracoli (es. `python3 esegui.py matrice ciclo`)

Prima di partire controlla i prerequisiti: l'API risponde e l'amministratore entra,
i contenitori d'esempio e il ClickHouse del motore ci sono, l'immagine dbt c'è (se
manca la costruisce), le tabelle dei casi (schema.sql) sono nel Postgres d'esempio.
Ogni oracolo scrive il suo log in E2E_LAVORO; alla fine un riassunto, ed esce con
errore se un oracolo non è verde."""
import os, re, subprocess, sys, time

from ambiente import CH_CONTENITORE, DOVE, IMMAGINE, PG_CONTENITORE, QUI, BASE
import comune

SUITE = {
    "veloce": ["codici", "giro", "sicurezza"],
    "completo": ["codici", "giro", "sicurezza", "matrice", "ciclo", "cartella", "flussi_sample", "deterministico"],
    "browser": ["browser_dialogo"],
    "ai": ["ai_descrizioni", "ai_traduzione"],
}
SUITE["tutto"] = SUITE["completo"] + SUITE["browser"] + SUITE["ai"]


def prerequisiti() -> list[str]:
    problemi = []
    try:
        comune.entra()
    except Exception as e:  # noqa: BLE001
        problemi.append(f"l'API {BASE} non risponde o l'amministratore non entra ({e}): TABULARIA_URL, AUTH__ADMIN_EMAIL/PASSWORD")
    for nome in (PG_CONTENITORE, CH_CONTENITORE):
        if subprocess.run(["docker", "inspect", nome], capture_output=True).returncode != 0:
            problemi.append(f"il contenitore «{nome}» non c'è (E2E_PG / E2E_CH_MOTORE)")
    if subprocess.run(["docker", "image", "inspect", IMMAGINE], capture_output=True).returncode != 0:
        print(f"l'immagine {IMMAGINE} manca: la costruisco da Dockerfile.dbt…", flush=True)
        r = subprocess.run(["docker", "build", "-q", "-f", os.path.join(QUI, "Dockerfile.dbt"), "-t", IMMAGINE, QUI], capture_output=True, text=True)
        if r.returncode != 0:
            problemi.append(f"l'immagine dbt non si costruisce: {r.stderr[-400:]}")
    if not problemi:
        r = comune.postgres_esempio(open(os.path.join(QUI, "schema.sql")).read())
        if r.returncode != 0:
            problemi.append(f"le tabelle dei casi non si creano nel Postgres d'esempio: {r.stderr[-300:]}")
    return problemi


def lancia(nome: str) -> tuple[bool, str, float]:
    inizio = time.time()
    log = os.path.join(DOVE, f"{nome}.log")
    with open(log, "w") as f:
        r = subprocess.run([sys.executable, os.path.join(QUI, f"{nome}.py")], cwd=QUI, stdout=f, stderr=subprocess.STDOUT)
    testo = open(log).read()
    conto = re.findall(r"(\d+)/(\d+) verifiche passate", testo)
    sintesi = f"{conto[-1][0]}/{conto[-1][1]}" if conto else "nessun conteggio"
    return r.returncode == 0, sintesi, time.time() - inizio


def main():
    argomenti = sys.argv[1:] or ["completo"]
    nomi = []
    for a in argomenti:
        nomi += SUITE.get(a, [a])
    sconosciuti = [n for n in nomi if not os.path.exists(os.path.join(QUI, f"{n}.py"))]
    if sconosciuti:
        sys.exit(f"oracoli sconosciuti: {sconosciuti}. Suite: {', '.join(SUITE)}")
    problemi = prerequisiti()
    if problemi:
        print("prerequisiti mancanti:\n  - " + "\n  - ".join(problemi))
        sys.exit(2)
    esiti = []
    for n in nomi:
        print(f"→ {n} …", end=" ", flush=True)
        verde, sintesi, durata = lancia(n)
        esiti.append((n, verde, sintesi))
        print(f"{'ok' if verde else 'NO'}  {sintesi}  ({durata / 60:.1f} min)  log: {os.path.join(DOVE, n + '.log')}", flush=True)
    rossi = [n for n, verde, _ in esiti if not verde]
    print(f"\n{len(esiti) - len(rossi)}/{len(esiti)} oracoli verdi" + (f" — da guardare: {', '.join(rossi)}" if rossi else ""))
    sys.exit(1 if rossi else 0)


if __name__ == "__main__":
    main()
