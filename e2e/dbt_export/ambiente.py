"""Dove girano gli oracoli dell'export dbt: tutto quello che dipende
dall'installazione, letto da variabili d'ambiente. I valori di default sono quelli
dello stack di prova descritto nel README (compose di produzione con il profilo
`samples`, API dietro il proxy sulla 8088, motore ClickHouse in un contenitore).

  TABULARIA_URL            l'interfaccia (default http://localhost:8088); l'API è sotto /api
  TABULARIA_API            l'API, se non è sotto /api dell'interfaccia
  TABULARIA_ENV            un file .env da cui leggere ciò che non è nell'ambiente
                           (AUTH__ADMIN_EMAIL/PASSWORD, CLICKHOUSE_EXTERNAL__*)
  E2E_RETE                 la rete Docker dello stack (default tabularia_interna)
  E2E_PG                   il contenitore del Postgres d'esempio (default tabularia-sampledb-postgres-1)
  E2E_CH_ESEMPIO           il contenitore del ClickHouse d'esempio, il CRM (default tabularia-sampledb-clickhouse-1)
  E2E_CH_MOTORE            il contenitore del ClickHouse del motore (default: CLICKHOUSE_EXTERNAL__HOST)
  E2E_IMMAGINE_DBT         l'immagine con dbt (default tabularia-dbt-e2e, vedi Dockerfile.dbt)
  E2E_LAVORO               dove scrivere i progetti esportati (default .lavoro/ qui accanto, ignorata da git)
  E2E_RIAVVIO              un comando che riavvia gateway ed engine (per l'oracolo del determinismo);
                           senza, quella parte si salta
"""
import os

QUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(QUI))


def _file_env() -> dict:
    percorso = os.environ.get("TABULARIA_ENV")
    if not percorso or not os.path.exists(percorso):
        return {}
    out = {}
    for riga in open(percorso):
        if "=" in riga and not riga.lstrip().startswith("#"):
            k, v = riga.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_FILE = _file_env()


def env(chiave: str, default: str | None = None) -> str | None:
    """Prima l'ambiente, poi il file TABULARIA_ENV, poi il default."""
    return os.environ.get(chiave) or _FILE.get(chiave) or default


UI = (env("TABULARIA_URL") or "http://localhost:8088").rstrip("/")
BASE = (env("TABULARIA_API") or UI + "/api").rstrip("/")
RETE = env("E2E_RETE", "tabularia_interna")
PG_CONTENITORE = env("E2E_PG", "tabularia-sampledb-postgres-1")
CH_ESEMPIO_CONTENITORE = env("E2E_CH_ESEMPIO", "tabularia-sampledb-clickhouse-1")
CH_CONTENITORE = env("E2E_CH_MOTORE") or env("CLICKHOUSE_EXTERNAL__HOST") or "chmotore"
CH_PORTA = env("CLICKHOUSE_EXTERNAL__PORT", "8123")
IMMAGINE = env("E2E_IMMAGINE_DBT", "tabularia-dbt-e2e")
DOVE = env("E2E_LAVORO") or os.path.join(QUI, ".lavoro")
RIAVVIO = env("E2E_RIAVVIO")
# da quale contenitore leggere la password di una connessione d'esempio, per tipo
PASSWORD_DA = {"postgresql": (PG_CONTENITORE, "POSTGRES_PASSWORD"), "clickhouse": (CH_ESEMPIO_CONTENITORE, "CLICKHOUSE_PASSWORD")}
os.makedirs(DOVE, exist_ok=True)
