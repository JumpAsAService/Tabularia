"""RBAC sul data plane: chi può leggere quale oggetto dello storage.

Le richieste inoltrate all'engine referenziano chiavi di storage (input_key,
driver dei foreach, export…). Prima di inoltrare, OGNI chiave "managed" trovata
nel payload deve risultare leggibile dall'utente secondo queste regole:

- upload propri (registro `uploads`): un upload appena fatto non vive in nessun
  progetto → lo legge solo chi l'ha caricato;
- datasource di catalogo: VIEW sul progetto della datasource;
- input/output dei run: chi ha lanciato il run, oppure VIEW sul progetto del
  flusso (la cronologia condivisa include i suoi output);
- chiavi referenziate dalla `definition` di un flusso salvato: VIEW sul progetto
  del flusso — condividere un flusso significa condividere i dati che
  referenzia, come i flussi impacchettati di Tableau.

Fail-closed: una chiave managed che non rientra in nessuna regola → 403 (vale
anche per `cache/`, che è interna all'engine e non deve mai arrivare dai client).
"""
from typing import Any, Iterable

from fastapi import HTTPException
from sqlmodel import Session, select
from sqlalchemy import or_

from app.models import Datasource, Flow, Run, Upload, User
from app.services import permissions as perm_service

# prefissi dello storage gestito (tutto il resto non è indirizzabile dai client)
MANAGED_PREFIXES = ("datasets/", "out/", "raw/", "cache/")

# campi che indirizzano una sorgente in LETTURA (top-level `input_key`, e `key`
# nelle sotto-sorgenti di join/union/foreach); l'output non è una lettura
_READ_KEY_FIELDS = ("key", "input_key")


def collect_read_refs(payload: Any) -> tuple[set[str], set[str]]:
    """Bucket e chiavi di LETTURA in posizione STRUTTURALE (campi key/input_key
    e bucket), ovunque nel payload. Diverso da collect_storage_keys, che raccoglie
    qualsiasi stringa con prefisso gestito: qui contano le POSIZIONI, così una
    chiave fuori dai prefissi gestiti in un campo sorgente è visibile e rifiutabile."""
    buckets: set[str] = set()
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str):
                    if k in _READ_KEY_FIELDS:
                        keys.add(v)
                    elif k == "bucket":
                        buckets.add(v)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(payload)
    return buckets, keys


def ensure_reads_pinned(user: User, payload: Any, engine_bucket: str) -> None:
    """Ogni sorgente di un non-superuser deve stare NEL bucket dell'engine e sotto
    un prefisso gestito. Senza questo, né gateway né engine vincolano bucket/chiave
    e l'engine (credenziali che leggono tutto) servirebbe qualsiasi (bucket, key)."""
    if user.is_superuser:
        return
    buckets, keys = collect_read_refs(payload)
    for b in buckets:
        if b and b != engine_bucket:
            raise HTTPException(status_code=403, detail=f"bucket non consentito: '{b}'")
    for k in keys:
        if k and not k.startswith(MANAGED_PREFIXES):
            raise HTTPException(status_code=403, detail=f"sorgente fuori dalle aree gestite: '{k}'")


def collect_storage_keys(payload: Any) -> set[str]:
    """Tutte le stringhe che sembrano chiavi managed, ovunque nel payload
    (anche nei parametri annidati delle operazioni, es. il driver dei foreach)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith(MANAGED_PREFIXES):
                found.add(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _can_read_key(session: Session, user: User, key: str, readable: set[int]) -> bool:
    if session.exec(
        select(Upload.id).where(Upload.parquet_key == key, Upload.owner_id == user.id)
    ).first():
        return True

    if readable and session.exec(
        select(Datasource.id).where(Datasource.key == key, Datasource.project_id.in_(readable))
    ).first():
        return True

    for run in session.exec(
        select(Run).where(or_(Run.output_key == key, Run.input_key == key))
    ).all():
        if run.launched_by == user.id:
            return True
        if run.flow_id and readable:
            flow = session.get(Flow, run.flow_id)
            if flow and flow.project_id in readable:
                return True

    # LIKE sulla definition dei flussi leggibili: le chiavi sono UUID esadecimali,
    # falsi positivi di fatto impossibili. Scala coi flussi salvati: se un giorno
    # diventasse un collo di bottiglia → tabella di riferimenti chiave↔flusso.
    if readable and session.exec(
        select(Flow.id).where(Flow.project_id.in_(readable), Flow.definition.contains(key))  # type: ignore[attr-defined]
    ).first():
        return True

    return False


def ensure_can_read_keys(session: Session, user: User, keys: Iterable[str]) -> None:
    """403 alla prima chiave managed non leggibile dall'utente."""
    keys = set(keys)
    if user.is_superuser or not keys:
        return
    readable = perm_service.readable_project_ids(session, user)
    for key in sorted(keys):
        if not _can_read_key(session, user, key, readable):
            raise HTTPException(status_code=403, detail=f"Non hai accesso all'oggetto '{key}'")
