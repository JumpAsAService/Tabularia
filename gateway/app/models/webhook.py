"""Webhook: Tabularia chiama un indirizzo esterno quando un'esecuzione finisce.

`Webhook` è l'abbonamento (dove, a quali eventi, per quale cartella);
`WebhookDelivery` è una consegna: nasce nella stessa transazione che chiude il
run (non si perde se il processo muore subito dopo) e la spedisce un ciclo degli
orchestratori, con nuovi tentativi a distanza crescente. Vedi services/webhooks.py.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, SQLModel


class Webhook(SQLModel, table=True):
    __tablename__ = "webhooks"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120)
    url: str = Field(max_length=2000)
    # il segreto con cui si firma ogni consegna: cifrato (Fernet) perché serve
    # in chiaro a ogni firma; si mostra solo alla creazione e alla rigenerazione
    secret_enc: str = Field(sa_column=Column(Text, nullable=False))
    # eventi a cui è abbonato, JSON (lista di nomi, vedi webhooks.EVENTI)
    events: str = Field(sa_column=Column(Text, nullable=False))
    # solo gli eventi di questa cartella e delle sue sottocartelle; nessuna = tutte
    project_id: Optional[int] = Field(default=None, foreign_key="projects.id")
    active: bool = True
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime


class WebhookDelivery(SQLModel, table=True):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # la coda: quelle da spedire, in ordine di quando sono dovute
        Index(
            "ix_webhook_deliveries_due", "next_attempt_at",
            postgresql_where=text("status = 'pending'"), sqlite_where=text("status = 'pending'"),
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # senza chiave esterna, apposta: la consegna nasce nella transazione che chiude
    # un run, e un webhook tolto in quell'istante farebbe fallire la CHIUSURA del
    # run. Una consegna di un webhook che non c'è più si chiude da sé («tolto»).
    webhook_id: int = Field(index=True)
    event: str = Field(max_length=64)
    # il corpo esatto che si spedisce (e si firma): uguale a ogni tentativo
    payload: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="pending", max_length=16)  # pending | sending | delivered | failed
    attempts: int = 0
    next_attempt_at: datetime
    created_at: datetime = Field(index=True)
    delivered_at: Optional[datetime] = None
    last_status_code: Optional[int] = None
    last_error: Optional[str] = Field(default=None, max_length=500)
    # quando un processo l'ha presa per spedirla: se muore a metà, oltre un
    # tempo la consegna torna in coda (almeno una consegna, mai zero)
    claimed_at: Optional[datetime] = None
