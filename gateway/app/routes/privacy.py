"""Informativa sulla privacy: la legge chiunque sia autenticato, la scrive l'admin.

Il testo di partenza NON è un modulo generico riempito di «ove applicabile»:
descrive quello che questa installazione raccoglie davvero, ruolo per ruolo. Chi
la adotta resta libero di riscriverla — è lui il titolare — ma parte da qualcosa
di vero invece che da un segnaposto.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import PrivacyNotice, User
from app.models.privacy import _now
from app.schemas.models import UtcDateTime
from app.services import audit

router = APIRouter(tags=["privacy"])

SOMMARIO_PREDEFINITO = (
    "What this installation records about you — and what it deliberately does not."
)

TESTO_PREDEFINITO = """\
## What we record about your account

- The email address and the name entered when the account was created.
- When you were last active, so that administrators can see who is connected right now.
- What you do: one audit entry per action — what it was, what it touched, and when.

## If you are signed in as a guest (read-only)

- **We do not record your IP address.**
- **We do not record your browser or your operating system.**
- **Your questions to the assistant are not saved.** Only the model, the token counts and the cost are kept, so that the daily spending limit keeps working.
- Where other people's accounts are listed, addresses are shown masked, for example `a***@example.com`.

## If you are signed in with a full account

- Each action is recorded together with your IP address and your browser.
- Conversations with the assistant are saved — question, answer and cost — until you delete them.

## Where the data goes

- The data you prepare stays in this installation's storage and in the engine chosen for the flow.
- The assistant is the exception: your question and the rows it reads are sent to the model provider configured here.

## How long it is kept

- Automatic deletion of audit entries: {audit_retention}.
- Deleting an account removes its content and its conversations, but not its audit entries — the record of what was done has to outlive the account that did it.

## Who to ask

Set a contact here for access, correction and erasure requests.
"""


def _durata(minuti: int) -> str:
    """La finestra di conservazione detta come la direbbe una persona."""
    if minuti <= 0:
        return "none — entries are kept until an administrator removes them"
    if minuti % 1440 == 0:
        giorni = minuti // 1440
        return f"{giorni} day" if giorni == 1 else f"{giorni} days"
    if minuti % 60 == 0:
        ore = minuti // 60
        return f"{ore} hour" if ore == 1 else f"{ore} hours"
    return f"{minuti} minute" if minuti == 1 else f"{minuti} minutes"


def rendi(testo: str) -> str:
    """Sostituisce i segnaposto col valore VERO della configurazione.

    Serve a una cosa sola: che l'informativa non possa mentire. Se domani
    qualcuno cambia `AUDIT__RETENTION_MINUTES` e si dimentica di riscrivere il
    testo, il testo si aggiorna da solo — perché il numero non è scritto lì
    dentro, è letto da dove la decisione vive davvero.

    Un segnaposto sconosciuto resta com'è invece di far saltare la pagina: chi
    scrive un'informativa non deve rischiare di romperla con una graffa.
    """
    return testo.replace("{audit_retention}", _durata(get_settings().audit.retention_minutes))


class PrivacyOut(BaseModel):
    enabled: bool
    # con i segnaposto già sostituiti: è ciò che si mostra
    summary: str
    body: str
    # come li ha scritti l'amministratore: è ciò che si modifica
    summary_template: str
    body_template: str
    url: str
    updated_at: UtcDateTime | None = None


class PrivacyUpdate(BaseModel):
    enabled: bool | None = None
    summary: str | None = None
    body: str | None = None
    url: str | None = None


def _out(n: PrivacyNotice) -> "PrivacyOut":
    return PrivacyOut(
        enabled=n.enabled,
        summary=rendi(n.summary), body=rendi(n.body),
        summary_template=n.summary, body_template=n.body,
        url=n.url, updated_at=n.updated_at,
    )


def _leggi(session: Session) -> PrivacyNotice:
    """La riga singola, creata alla prima lettura col testo di partenza."""
    nota = session.exec(select(PrivacyNotice)).first()
    if nota is None:
        nota = PrivacyNotice(id=1, enabled=True, summary=SOMMARIO_PREDEFINITO,
                             body=TESTO_PREDEFINITO, url="")
        session.add(nota)
        session.commit()
        session.refresh(nota)
    return nota


@router.get("/privacy-notice", response_model=PrivacyOut)
def get_privacy(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    n = _leggi(session)
    return _out(n)


@router.put("/privacy-notice", response_model=PrivacyOut)
def update_privacy(
    body: PrivacyUpdate,
    current: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    n = _leggi(session)
    for campo in ("enabled", "summary", "body", "url"):
        valore = getattr(body, campo)
        if valore is not None:
            setattr(n, campo, valore)
    n.updated_at = _now()
    n.updated_by = current.id
    session.add(n)
    session.commit()
    session.refresh(n)
    # cambiare l'informativa cambia ciò che l'installazione DICHIARA a tutti:
    # è una modifica che deve lasciare traccia come le altre
    audit.record_audit(session, actor=current, action=audit.PRIVACY_UPDATE,
                       target_type="privacy_notice", target_id=n.id,
                       detail={"enabled": n.enabled})
    return _out(n)
