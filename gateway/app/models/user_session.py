"""Una sessione aperta: un accesso riuscito da un browser o da uno script.

Il token resta un JWT firmato, ma porta l'id di una di queste righe (`sid`) e il
gateway la cerca a ogni richiesta, nella stessa lettura dell'utente. Finché la
riga c'è il token vale; toglierla è il modo di chiudere la sessione: l'uscita,
la revoca da parte dell'amministratore, il cambio di password, la scadenza. Per
questo non c'è una colonna «revocata»: una sessione chiusa non serve a niente e
non deve tenersi addosso l'indirizzo di chi l'aveva aperta.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"
    # casuale (secrets.token_urlsafe): da solo non apre niente, serve la firma del JWT
    id: str = Field(primary_key=True, max_length=64)
    user_id: int = Field(foreign_key="users.id", index=True)
    # tutte naive UTC, come le altre colonne
    created_at: datetime
    # l'ultima richiesta vista, aggiornata al più una volta al minuto: decide il
    # timeout per inattività
    last_seen_at: datetime
    # il tetto assoluto: oltre, si rientra comunque
    expires_at: datetime = Field(index=True)
    # da dove e con che cosa è entrata; vuoti per un OSSERVATORE (vedi
    # permissions.is_observer_only)
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, max_length=512)
    # "password" o "sso"
    method: str = Field(default="password", max_length=16)
