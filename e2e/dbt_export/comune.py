"""Gli aiuti comuni agli oracoli dell'export dbt: l'API di Tabularia, dbt VERO in un
contenitore usa-e-getta (immagine E2E_IMMAGINE_DBT) sulla rete dello stack, l'export
di un flusso, il confronto di un modello dbt con la datasource che Tabularia pubblica
(righe e, colonna per colonna, somma per i numeri e valori distinti per il resto).
La configurazione è in ambiente.py."""
import csv, io, json, os, re, subprocess, sys, urllib.error, urllib.request, zipfile

from ambiente import BASE, DOVE, IMMAGINE, PASSWORD_DA, RETE, env

esiti = []
TOK = None


def ok(nome, cond, dettaglio=""):
    esiti.append(bool(cond))
    print(("  ok " if cond else "  ✗  ") + nome + ("" if cond else f"  → {str(dettaglio)[:900]}"), flush=True)
    return bool(cond)




def api(m, p, c=None, grezzo=False, multipart=None):
    if multipart:
        confine = "----tabularia-prova"
        corpo = b""
        for nome, (fname, dati, tipo) in multipart.items():
            corpo += f"--{confine}\r\nContent-Disposition: form-data; name=\"{nome}\"; filename=\"{fname}\"\r\nContent-Type: {tipo}\r\n\r\n".encode() + dati + b"\r\n"
        corpo += f"--{confine}--\r\n".encode()
        testate = {"content-type": f"multipart/form-data; boundary={confine}"}
    else:
        corpo = json.dumps(c).encode() if c is not None else None
        testate = {"content-type": "application/json"}
    if TOK:
        testate["authorization"] = f"Bearer {TOK}"
    req = urllib.request.Request(BASE + p, method=m, data=corpo, headers=testate)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            x = r.read(); return r.status, (x if grezzo else (json.loads(x) if x else None))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:600]


def entra():
    global TOK
    TOK = api("POST", "/auth/login", {"email": env("AUTH__ADMIN_EMAIL"), "password": env("AUTH__ADMIN_PASSWORD")})[1]["access_token"]


def env_del_contenitore(nome, chiave):
    out = subprocess.run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", nome], capture_output=True, text=True).stdout
    return next((r.split("=", 1)[1] for r in out.splitlines() if r.startswith(chiave + "=")), None)


def password_delle_connessioni():
    # target clickhouse: dbt entra nel ClickHouse del motore con un utente suo (qui quello del motore di scarto)
    variabili = {"CLICKHOUSE_USER": env("CLICKHOUSE_EXTERNAL__USERNAME") or "", "CLICKHOUSE_PASSWORD": env("CLICKHOUSE_EXTERNAL__PASSWORD") or ""}
    for c in api("GET", "/connections")[1]:
        cont, chiave = PASSWORD_DA.get(c["db_type"], (None, None))
        if cont:
            variabili[f"TABULARIA_DB_{c['id']}_PASSWORD"] = env_del_contenitore(cont, chiave) or ""
    return variabili


def firma(righe):
    """{colonna: somma arrotondata se tutti numeri, altrimenti numero di valori distinti} + righe."""
    if not righe:
        return {"righe": 0}
    out = {"righe": len(righe)}
    for col in righe[0]:
        vals = [{"true": 1, "false": 0}.get(str(r[col]).lower(), r[col]) for r in righe]   # booleani: dbt dà true/false, il CSV "true"/"false"
        try:
            nums = [float(v) for v in vals if v not in (None, "")]
            out[col] = ("somma", round(sum(nums), 2), len(vals) - len(nums))
        except (TypeError, ValueError):
            out[col] = ("distinti", len({str(v) for v in vals}), sum(1 for v in vals if v in (None, "")))
    return out


def dbt(cartella, *argomenti, variabili=None):
    # con il MIO utente, così i file che dbt scrive (target/, logs/) restano miei
    cmd = ["docker", "run", "--rm", "--network", RETE, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{cartella}:/progetto", "-w", "/progetto", "-e", "DBT_PROFILES_DIR=/progetto"]
    for k, v in (variabili or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [IMMAGINE, "dbt", *argomenti, "--no-use-colors"], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def esporta(flusso, target, suffisso=""):
    """Scarica lo zip dell'export e lo apre in una cartella pulita: (cartella, nomi dei file) o (None, errore)."""
    st, zipped = api("GET", f"/flows/{flusso['id']}/export/dbt?target={target}", grezzo=True)
    if st != 200 or zipped[:2] != b"PK":
        return None, (st, zipped[:400] if isinstance(zipped, (bytes, str)) else zipped)
    slug = re.sub(r"[^a-z0-9]+", "_", flusso["name"].lower()).strip("_")
    cartella = f"{DOVE}/{slug}_{target}{suffisso}"
    if os.path.exists(cartella):
        subprocess.run(["docker", "run", "--rm", "-v", f"{DOVE}:/x", IMMAGINE, "sh", "-c", f"rm -rf /x/{os.path.basename(cartella)}"], capture_output=True)
    os.makedirs(cartella)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        z.extractall(cartella)
        return cartella, sorted(z.namelist())


def righe_del_modello(cartella, modello, variabili):
    rc, out = dbt(cartella, "show", "--inline", "select * from {{ ref('%s') }}" % modello, "--limit", "200000", "--output", "json", "--quiet", variabili=variabili)
    inizio = out.index("{"); dati = json.loads(out[inizio:])
    return dati.get("show") or dati.get("data") or []


def righe_della_datasource(d):
    st, csv_bytes = api("POST", "/tasks/export", {"bucket": d["bucket"], "input_key": d["key"], "operations": [], "format": "csv", "filename": "x.csv"}, grezzo=True)
    if st != 200:
        raise RuntimeError(f"export CSV della datasource: {st} {csv_bytes}")
    return list(csv.DictReader(io.StringIO(csv_bytes.decode())))


def confronta(cartella, modello, datasource, variabili):
    """Il modello dbt ↔ la datasource di Tabularia: stessa firma?"""
    try:
        f1 = firma(righe_del_modello(cartella, modello, variabili))
    except Exception as e:  # noqa: BLE001
        return ok(f"righe del modello «{modello}» leggibili", False, e)
    f2 = firma(righe_della_datasource(datasource))
    chiavi = sorted(set(f1) | set(f2))
    return ok(f"«{modello}»: dbt e Tabularia danno lo stesso risultato ({f2['righe']} righe, {len(chiavi) - 1} colonne)",
              all(f1.get(k) == f2.get(k) for k in chiavi),
              "\n".join(f"    {k}: dbt={f1.get(k)} tabularia={f2.get(k)}" for k in chiavi if f1.get(k) != f2.get(k)))


def slug_modello(nome):
    return re.sub(r"[^a-z0-9_]", "_", nome.lower()).strip("_")


# ── il ClickHouse del motore (target clickhouse) ─────────────────────────────
def clickhouse_motore(sql: str) -> subprocess.CompletedProcess:
    """SQL al ClickHouse del motore, dall'INPUT del client: dentro `sh -c "…"` i
    backtick dei nomi sarebbero sostituzioni di comando della shell."""
    from ambiente import CH_CONTENITORE

    cmd = ["docker", "exec", "-i", CH_CONTENITORE, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery']
    return subprocess.run(cmd, input=sql, capture_output=True, text=True)


def database_clickhouse() -> set:
    return set(clickhouse_motore("SELECT name FROM system.databases FORMAT TSV").stdout.split())


def pulisci_clickhouse():
    """Via i database di dbt e delle sorgenti federate, e si verifica che non ci siano più."""
    nomi = [n for n in database_clickhouse() if n == "dbt_tabularia" or n.startswith("tab_src_")]
    if nomi:
        r = clickhouse_motore("".join(f"DROP DATABASE IF EXISTS `{n}`;\n" for n in nomi))
        if r.returncode != 0:
            raise RuntimeError(f"pulizia di ClickHouse non riuscita: {r.stderr[-300:]}")
    rimasti = [n for n in database_clickhouse() if n == "dbt_tabularia" or n.startswith("tab_src_")]
    if rimasti:
        raise RuntimeError(f"pulizia di ClickHouse: restano {rimasti}")


def postgres_esempio(sql: str) -> subprocess.CompletedProcess:
    """SQL al Postgres d'esempio, dall'input di psql."""
    from ambiente import PG_CONTENITORE

    return subprocess.run(["docker", "exec", "-i", PG_CONTENITORE, "sh", "-c", 'psql -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                          input=sql, capture_output=True, text=True)


def aspetta_run(rid, secondi=900):
    import time

    fine = time.time() + secondi
    while time.time() < fine:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r.get("status") in ("SUCCESS", "FAILURE", "REVOKED"):
            return r
        time.sleep(2)
    return {"status": "TIMEOUT"}


def riepilogo():
    print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
    sys.exit(0 if all(esiti) else 1)
