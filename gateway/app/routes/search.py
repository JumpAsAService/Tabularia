"""Ricerca unificata per NOME su tutte le risorse del catalogo.

Esistono già `/flows/search`, `/datasources/search` e `/connections/search`, ma
ciascuna risponde per un tipo solo e due tipi non ne avevano affatto (cartelle e
viste salvate). Chi cerca «margine» non sa, e non deve sapere, se quello che
cerca è un flusso o una vista: questa rotta risponde alla domanda vera.

**Perché non una UNION SQL.** I cinque tipi vivono in tabelle senza colonne in
comune e — soprattutto — con permessi DIVERSI: le cartelle usano l'insieme
navigabile (`visible_project_ids`, che include gli antenati per mostrare il
percorso), i contenuti usano quello leggibile (`readable_project_ids`), e le
connessioni una capability ORTOGONALE (CONNECT: chi ha solo VIEW non le vede).
Una UNION dovrebbe portarsi dietro tre filtri diversi in un unico statement.
Cinque query filtrate e un'unione in Python dicono la stessa cosa in modo
leggibile, ed è la scelta che questo codice fa già altrove (vedi
services/permissions.py, che risolve l'albero in Python di proposito).

**Si cerca solo nel NOME.** È ciò che serve, ed è prevedibile: un risultato che
compare per una corrispondenza in un campo che l'utente non vede è un risultato
che sembra un errore.
"""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import Connection, Datasource, Flow, Project, SavedView, User
from app.schemas.models import SearchHit, SearchOut
from app.services import permissions as perm_service

router = APIRouter(tags=["search"])

# ordine di presentazione a parità di rilevanza: prima i contenitori, poi ciò che
# si apre più spesso. Serve anche a rendere deterministico l'ordinamento.
_KIND_ORDER = {"folder": 0, "flow": 1, "datasource": 2, "view": 3, "connection": 4}


def _like(q: str) -> str:
    """Testo cercato → pattern LIKE, con gli speciali resi letterali: chi cerca
    `a_b` intende `a_b`, non «a, un carattere qualsiasi, b».

    L'escape va DICHIARATO a ogni `ilike(..., escape="\\\\")`: in PostgreSQL la
    barra rovescia è già l'escape predefinito di LIKE, in SQLite no. Senza
    dichiararlo la stessa ricerca darebbe risultati diversi fra i test (SQLite) e
    la produzione (Postgres), che è il tipo di divergenza peggiore: verde qui,
    sbagliata sul campo.
    """
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/search", response_model=SearchOut)
def search_everything(
    q: str = Query("", description="cerca per nome su tutte le risorse"),
    kind: str | None = Query(None, description="limita a un tipo: folder|flow|datasource|view|connection"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    termine = q.strip()
    if not termine:
        return SearchOut(items=[], total=0, counts={})

    pattern = _like(termine)
    leggibili = perm_service.readable_project_ids(session, user)
    navigabili = perm_service.visible_project_ids(session, user)
    connettibili = perm_service.connectable_project_ids(session, user)

    # nomi delle cartelle: servono a dire DOVE si trova ogni risultato
    nomi_cartella = {
        p.id: p.name for p in session.exec(select(Project)).all()
    }

    hits: list[SearchHit] = []

    if navigabili:
        for p in session.exec(
            select(Project).where(Project.id.in_(navigabili), Project.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="folder", id=p.id, name=p.name,
                project_id=p.parent_id, project_name=nomi_cartella.get(p.parent_id),
            ))

    if leggibili:
        for f in session.exec(
            select(Flow).where(Flow.project_id.in_(leggibili), Flow.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="flow", id=f.id, name=f.name,
                project_id=f.project_id, project_name=nomi_cartella.get(f.project_id),
                detail=f.engine,
            ))

        for d in session.exec(
            select(Datasource).where(Datasource.project_id.in_(leggibili), Datasource.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="datasource", id=d.id, name=d.name,
                project_id=d.project_id, project_name=nomi_cartella.get(d.project_id),
                detail=f"{d.rows} righe" if d.rows is not None else "",
            ))

        for v in session.exec(
            select(SavedView).where(SavedView.project_id.in_(leggibili), SavedView.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="view", id=v.id, name=v.name,
                project_id=v.project_id, project_name=nomi_cartella.get(v.project_id),
            ))

    # CONNECT è ortogonale a VIEW: chi legge una cartella non ne vede per forza
    # le connessioni, quindi l'insieme è un altro
    if connettibili:
        for c in session.exec(
            select(Connection).where(Connection.project_id.in_(connettibili), Connection.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="connection", id=c.id, name=c.name,
                project_id=c.project_id, project_name=nomi_cartella.get(c.project_id),
                detail=c.db_type,
            ))

    # i conteggi descrivono TUTTO ciò che combacia, non la pagina corrente: è il
    # numero che va nel selettore per tipo
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.kind] = counts.get(h.kind, 0) + 1

    if kind:
        hits = [h for h in hits if h.kind == kind]

    # rilevanza minima ma reale: chi COMINCIA col termine cercato viene prima.
    # Senza, cercare "ordini" mette "riordini_2024" sopra "ordini".
    minuscolo = termine.lower()
    hits.sort(key=lambda h: (
        0 if h.name.lower().startswith(minuscolo) else 1,
        _KIND_ORDER.get(h.kind, 9),
        h.name.lower(),
    ))

    return SearchOut(items=hits[offset:offset + limit], total=len(hits), counts=counts)
