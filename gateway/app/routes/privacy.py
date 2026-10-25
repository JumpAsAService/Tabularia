"""Privacy notice: anyone authenticated reads it, the admin writes it.

The starting text is NOT a generic form filled with «where applicable»: it describes
what this installation really collects, role by role. Whoever adopts it stays free to
rewrite it — they are the controller — but starts from something true instead of from
a placeholder.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import require_superuser
from app.models import PrivacyNotice, User  # noqa: F401 — the PUT needs User
from app.models.privacy import _now
from app.schemas.models import UtcDateTime
from app.services import audit
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, tags=["privacy"])

DEFAULT_SUMMARY = (
    "What this installation records about you — and what it deliberately does not."
)

DEFAULT_TEXT = """\
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


def _duration(minutes: int) -> str:
    """The retention window said as a person would say it."""
    if minutes <= 0:
        return "none — entries are kept until an administrator removes them"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        ore = minutes // 60
        return f"{ore} hour" if ore == 1 else f"{ore} hours"
    return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"


def hand_back(text: str) -> str:
    """Replaces the placeholders with the configuration's TRUE value.

    It serves one thing only: that the notice cannot lie. If tomorrow someone changes
    `AUDIT__RETENTION_MINUTES` and forgets to rewrite the text, the text updates by
    itself — because the number is not written in there, it is read from where the
    decision really lives.

    An unknown placeholder stays as it is instead of breaking the page: whoever writes a
    notice must not risk breaking it with a brace.
    """
    return text.replace("{audit_retention}", _duration(get_settings().audit.retention_minutes))


class PrivacyOut(BaseModel):
    enabled: bool
    # with the placeholders replaced already: it is what is shown
    summary: str
    body: str
    # as the administrator wrote them: it is what is edited
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
        summary=hand_back(n.summary), body=hand_back(n.body),
        summary_template=n.summary, body_template=n.body,
        url=n.url, updated_at=n.updated_at,
    )


def _read(session: Session) -> PrivacyNotice:
    """The single row, created at the first read with the starting text."""
    note = session.exec(select(PrivacyNotice)).first()
    if note is None:
        note = PrivacyNotice(id=1, enabled=True, summary=DEFAULT_SUMMARY,
                             body=DEFAULT_TEXT, url="")
        session.add(note)
        session.commit()
        session.refresh(note)
    return note


@router.get("/privacy-notice", response_model=PrivacyOut)
def get_privacy(session: Session = Depends(get_session)):
    """PUBLIC, without authentication, and that is the point: the notice is read BEFORE
    signing in. Saying it only to whoever is in already would mean informing someone after
    taking their data.

    It exposes nothing not meant to be read by anyone: it is a text the organization
    publishes on purpose."""
    return _out(_read(session))


@router.put("/privacy-notice", response_model=PrivacyOut)
def update_privacy(
    body: PrivacyUpdate,
    current: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    n = _read(session)
    for field in ("enabled", "summary", "body", "url"):
        value = getattr(body, field)
        if value is not None:
            setattr(n, field, value)
    n.updated_at = _now()
    n.updated_by = current.id
    session.add(n)
    session.commit()
    session.refresh(n)
    # changing the notice changes what the installation DECLARES to everyone: it is a change
    # that must leave a trace like the others
    audit.record_audit(session, actor=current, action=audit.PRIVACY_UPDATE,
                       target_type="privacy_notice", target_id=n.id,
                       detail={"enabled": n.enabled})
    return _out(n)
