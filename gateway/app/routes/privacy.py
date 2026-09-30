"""Informativa sulla privacy: la legge chiunque sia autenticato, la scrive l'admin.

Il testo di partenza NON è un modulo generico riempito di «ove applicabile»:
descrive quello che questa installazione raccoglie davvero, ruolo per ruolo. Chi
la adotta resta libero di riscriverla — è lui il titolare — ma parte da qualcosa
di vero invece che da un segnaposto.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

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

- Audit entries are kept until an administrator removes them: there is no automatic expiry.
- Deleting an account removes its content and its conversations, but not its audit entries — the record of what was done has to outlive the account that did it.

## Who to ask

Set a contact here for access, correction and erasure requests.
"""


class PrivacyOut(BaseModel):
    enabled: bool
    summary: str
    body: str
    url: str
    updated_at: UtcDateTime | None = None


class PrivacyUpdate(BaseModel):
    enabled: bool | None = None
    summary: str | None = None
    body: str | None = None
    url: str | None = None


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
    return PrivacyOut(enabled=n.enabled, summary=n.summary, body=n.body, url=n.url,
                      updated_at=n.updated_at)


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
    return PrivacyOut(enabled=n.enabled, summary=n.summary, body=n.body, url=n.url,
                      updated_at=n.updated_at)
