"""Motori consentiti nell'installazione: lettura e modifica dal pannello admin.

Serve a standardizzare la flotta — «qui si progetta solo su ClickHouse» — senza
doverlo ripetere a ogni flusso.

Il contratto, scelto deliberatamente:

- disabilitare un motore impedisce di SCEGLIERLO (creazione, cambio motore,
  motore di produzione), ma NON ferma i flussi che lo usano già: un interruttore
  premuto qui non deve poter fermare un DAG schedulato mentre gira;
- perché la standardizzazione non resti cosmetica, ogni motore riporta quanti
  flussi lo usano ancora — è la lista di lavoro della migrazione, visibile
  all'amministratore invece che sepolta nel database;
- l'ULTIMO motore consentito non si può togliere: un'installazione senza motori
  non potrebbe più creare un flusso, e sarebbe un vicolo cieco raggiungibile con
  un clic.

Scrittura solo superuser, e ogni cambio finisce nell'audit: modifica ciò che
l'intera installazione può fare.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import require_superuser
from app.models import DisabledEngine, Flow, User
from app.schemas.models import EnginePolicyOut, EnginePolicyUpdate
from app.services import audit
from app.services.engine_policy import KNOWN_ENGINES, disabled_engines

router = APIRouter(prefix="/engine-policy", tags=["engine-policy"])


def _flows_using(session: Session, engine_id: str) -> int:
    """Flussi che userebbero ancora questo motore: quello di SVILUPPO o quello
    di PRODUZIONE: sono due campi distinti e un flusso può essere fuori standard
    per l'uno o per l'altro."""
    return session.exec(
        select(func.count())
        .select_from(Flow)
        .where(or_(Flow.engine == engine_id, Flow.production_engine == engine_id))
    ).one()


def _stato(session: Session) -> list[EnginePolicyOut]:
    vietati = disabled_engines(session)
    return [
        EnginePolicyOut(
            engine_id=e,
            allowed=e not in vietati,
            flows_using=_flows_using(session, e),
        )
        for e in KNOWN_ENGINES
    ]


@router.get("", response_model=list[EnginePolicyOut])
def list_engine_policy(
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    """Tutti i motori noti, con lo stato e quanti flussi li usano ancora."""
    return _stato(session)


@router.put("/{engine_id}", response_model=EnginePolicyOut)
def set_engine_policy(
    engine_id: str,
    body: EnginePolicyUpdate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    eid = engine_id.strip().lower()
    if eid not in KNOWN_ENGINES:
        raise HTTPException(status_code=404, detail=f"Motore sconosciuto: '{engine_id}'")

    vietati = disabled_engines(session)
    riga = session.get(DisabledEngine, eid)

    if body.allowed:
        if riga is not None:
            session.delete(riga)
            session.commit()
    else:
        # niente vicoli ciechi: se questo fosse l'ultimo consentito, nessuno
        # potrebbe più creare un flusso e nemmeno rimediare dall'interfaccia
        if len(vietati | {eid}) >= len(KNOWN_ENGINES):
            raise HTTPException(
                status_code=422,
                detail="Deve restare almeno un motore consentito: senza, non si potrebbe più creare un flusso.",
            )
        if riga is None:
            session.add(DisabledEngine(engine_id=eid, disabled_by=user.id))
            session.commit()

    usati = _flows_using(session, eid)
    audit.record_audit(
        session, actor=user,
        action=audit.ENGINE_ALLOW if body.allowed else audit.ENGINE_DISALLOW,
        target_type="engine", target_label=eid,
        # il conteggio va nel registro: dice quanti flussi restano fuori
        # standard NEL MOMENTO della decisione, che è l'informazione utile
        # a chi legge l'audit mesi dopo
        detail={"flows_using": usati}, request=request,
    )
    return EnginePolicyOut(engine_id=eid, allowed=body.allowed, flows_using=usati)
