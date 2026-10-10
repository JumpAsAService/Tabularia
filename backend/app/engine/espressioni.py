"""Il controllo delle espressioni del nodo compute: solo espressioni SCALARI.

Un'espressione del compute gira dentro il motore (ClickHouse, chDB…), con i suoi
permessi: non deve poter leggere altre tabelle, file, URL o dati del server.
Prima il controllo cercava PAROLE (`from`, `file`, `url`, …) e così rifiutava
anche espressioni innocue: `EXTRACT(YEAR FROM data)`, `TRIM(BOTH ' ' FROM x)`, o
una colonna che si chiama `url` (trovato il 2026-10-10: su ClickHouse, il motore
di quasi tutti i clienti, non si poteva estrarre l'anno da una data).

Ora l'espressione si ANALIZZA (sqlglot, nel dialetto del motore) e si rifiuta
quello che è davvero pericoloso:
  · una sottoquery, un FROM, una JOIN, un riferimento a una tabella — quindi ogni
    table function (`s3()`, `file()`, `url()`, `remote()`, `mysql()`, …);
  · le funzioni SCALARI che leggono dati fuori dalla riga: dizionari (`dictGet…`),
    `joinGet`, i modelli (`catboostEvaluate`, `evalMLMethod`), i metadati del server;
  · più espressioni in una (`;`).
Se l'espressione non si riesce a leggere, vale la regola prudente di prima (a
parole): meglio un rifiuto di troppo che una lettura non vista.
"""
from __future__ import annotations

import re

# il controllo a parole di prima: resta per ciò che sqlglot non sa leggere
_PAROLE_VIETATE = re.compile(
    r"\b(?:select|from|with|insert|attach|create|"
    r"file|url|s3|hdfs|remote|remoteSecure|mysql|postgresql|jdbc|odbc|"
    r"clusterAllReplicas|cluster|dictionary|merge|numbers|zeros)\b",
    re.IGNORECASE,
)

# funzioni scalari che leggono dati che non sono nella riga (o il server stesso)
_FUNZIONI_VIETATE = re.compile(
    r"^(?:dictget\w*|dicthas|dictisin|dictgethierarchy|dictgetchildren|dictgetdescendants|"
    r"joinget\w*|catboostevaluate|evalmlmethod|modelevaluate|hascolumnintable|"
    r"getsetting|getmacro|getserverport|filesystem\w*|"
    r"file|url|s3|s3cluster|hdfs|remote|remotesecure|mysql|postgresql|jdbc|odbc|sqlite|mongodb|redis|"
    r"azureblobstorage|gcs|iceberg\w*|deltalake|hudi|input|executable|"
    r"clusterallreplicas|cluster|dictionary|merge|numbers|zeros|generaterandom|view|"
    r"read_\w+|parquet_scan|scan_\w+|query|query_table)$",
    re.IGNORECASE,
)


def _nome_funzione(f) -> str:
    from sqlglot import exp

    if isinstance(f, exp.Anonymous):
        return str(f.this)
    return f.sql_name() if hasattr(f, "sql_name") else type(f).__name__


def espressione_vietata(expr: str, dialetto: str = "clickhouse") -> bool:
    """True se l'espressione del compute NON è una semplice espressione scalare."""
    import sqlglot
    from sqlglot import exp

    if ";" in expr:
        return True
    try:
        alberi = sqlglot.parse(f"SELECT ({expr}) AS _x", read=dialetto)
    except Exception:  # noqa: BLE001 — non si legge: regola prudente, a parole
        return bool(_PAROLE_VIETATE.search(expr))
    if len(alberi) != 1 or alberi[0] is None:
        return True
    albero = alberi[0]
    if not isinstance(albero, exp.Select) or albero.args.get("from") or albero.args.get("joins"):
        return True
    for nodo in albero.walk():
        if nodo is albero:
            continue
        if isinstance(nodo, (exp.Select, exp.Subquery, exp.From, exp.Join, exp.Table, exp.Union, exp.Insert, exp.Create, exp.Command)):
            return True
        if isinstance(nodo, exp.Func) and _FUNZIONI_VIETATE.match(_nome_funzione(nodo) or ""):
            return True
    return False
