"""Toglie i segreti dai testi che arrivano all'utente.

Serve perché l'engine mette le credenziali dello storage DENTRO il SQL: senza
una `named collection`, ogni sorgente diventa

    s3('<url>', '<access key>', '<secret key>', 'Parquet')

ClickHouse maschera i segreti analizzando la query, ma se la query non si
ANALIZZA non c'è niente da mascherare e il messaggio d'errore riporta il
frammento in chiaro (audit 2026-09-19, A2: riprodotto con chiavi finte). Da lì
il testo risale al toast dell'editor, a `Run.error_detail` salvato nel database
e, attraverso l'assistente, al fornitore del modello.

Si redige alla SORGENTE — nel processo che conosce i segreti — così ogni
consumatore a valle riceve testo già pulito senza doversene ricordare.

Non sostituisce la `named collection`, che resta la soluzione giusta: lì le
chiavi non entrano proprio nella query. Questa è la rete di sicurezza per le
installazioni che non la usano, e per qualunque segreto che un domani finisse
in un messaggio d'eccezione.
"""
from __future__ import annotations

import re

MASK = "***"
# sotto questa lunghezza non si redige: una "chiave" di pochi caratteri
# comparirebbe ovunque per caso e mangerebbe il messaggio
_MIN_LEN = 8


def _secrets() -> list[str]:
    from app.core.config import get_settings

    cfg = get_settings()
    grezzi: list[str] = []

    def aggiungi(v) -> None:
        if v is None:
            return
        testo = v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)
        if testo and len(testo) >= _MIN_LEN:
            grezzi.append(testo)

    aggiungi(cfg.storage.access_key)
    aggiungi(cfg.storage.secret_key)
    ch = getattr(cfg, "clickhouse_external", None)
    if ch is not None:
        aggiungi(getattr(ch, "password", None))
        aggiungi(getattr(ch, "ai_password", None))
    bq = getattr(cfg, "bigquery", None)
    if bq is not None:
        aggiungi(getattr(bq, "credentials_b64", None))
    # i più lunghi per primi: se un segreto ne contiene un altro, si redige
    # prima quello esterno e non restano frammenti
    return sorted(set(grezzi), key=len, reverse=True)


def redact_secrets(text: str | None) -> str | None:
    """Sostituisce con `***` ogni segreto noto trovato nel testo.

    Non solleva mai: un errore qui trasformerebbe un messaggio d'errore in un
    altro errore, e il chiamante sta già gestendo un guasto."""
    if not text:
        return text
    try:
        pulito = text
        for segreto in _secrets():
            if segreto in pulito:
                pulito = pulito.replace(segreto, MASK)
        return pulito
    except Exception:  # pragma: no cover — la redazione non deve mai propagare
        return text


# funzioni che toccano lo storage o la rete: dentro ci sono URL, bucket,
# percorsi e — su ClickHouse senza named collection — le chiavi
_NOMI_STORAGE = (
    "s3", "s3Cluster", "url", "urlCluster", "remote", "remoteSecure",
    "azureBlobStorage", "gcs", "hdfs",
)
_APERTURA = re.compile(r"\b(" + "|".join(_NOMI_STORAGE) + r")\s*\(", re.IGNORECASE)


def _fine_argomenti(testo: str, inizio: int) -> int | None:
    """Indice della parentesi che chiude quella aperta in `inizio`, o None se il
    testo FINISCE prima. Conta le parentesi solo fuori dagli apici, perché un URL
    o un percorso può contenerne."""
    profondita = 0
    apice = None
    i = inizio
    while i < len(testo):
        c = testo[i]
        if apice:
            if c == "\\":
                i += 2
                continue
            if c == apice:
                apice = None
        elif c in "'\"`":
            apice = c
        elif c == "(":
            profondita += 1
        elif c == ")":
            profondita -= 1
            if profondita == 0:
                return i
        i += 1
    return None


def redact_storage_args(text: str | None) -> str | None:
    """Svuota gli argomenti di `s3(...)`, `url(...)` e simili.

    Il `query_log` di ClickHouse maschera la chiave SEGRETA da sé, ma non la
    chiave di ACCESSO, e insieme a quella restano in chiaro l'endpoint, il bucket
    e il percorso dell'oggetto — verificato su un ClickHouse 24.8 il 2026-09-28.
    In un elenco di query lente quegli argomenti non dicono niente di utile: a
    chi guarda serve sapere QUALE query pesa, non su quale chiave.

    Due trappole, entrambe trovate dal vivo:

    - il testo può essere TRONCATO (il `query_log` si legge a fette): la
      parentesi non si chiude mai, e proprio lì dentro sta la chiave d'accesso.
      Senza chiusura si redige fino alla fine — meglio perdere la coda di una
      query che pubblicare una credenziale;
    - `s3(` può comparire DENTRO una stringa (`WHERE nome LIKE '%s3(%'`): lì non
      è una chiamata e non si tocca, altrimenti si rovina una query legittima.
    """
    if not text:
        return text
    try:
        fuori = []
        i = 0
        apice = None
        while i < len(text):
            c = text[i]
            if apice:                      # dentro una stringa: si copia e basta
                fuori.append(c)
                if c == "\\" and i + 1 < len(text):
                    fuori.append(text[i + 1])
                    i += 2
                    continue
                if c == apice:
                    apice = None
                i += 1
                continue
            if c in "'\"`":
                apice = c
                fuori.append(c)
                i += 1
                continue
            m = _APERTURA.match(text, i)
            if m:
                apre = m.end() - 1
                chiude = _fine_argomenti(text, apre)
                fuori.append(text[i:m.end()])
                fuori.append("\u2026)")
                if chiude is None:         # troncata: si taglia qui
                    return "".join(fuori)
                i = chiude + 1
                continue
            fuori.append(c)
            i += 1
        return "".join(fuori)
    except Exception:  # pragma: no cover
        return text
