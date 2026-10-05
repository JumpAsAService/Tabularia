"""Stato di breve durata che più processi del gateway devono vedere uguale.

Stava in dizionari in memoria finché il gateway era un processo solo. Con più
repliche ognuna avrebbe i suoi: cinque tentativi di accesso sbagliati per
replica invece che in tutto, e due persone sullo stesso flusso che non si vedono
perché parlano con repliche diverse. Sta su Postgres perché è lo store che il
gateway ha già: Valkey è dell'engine, e in produzione il gateway non ne riceve
nemmeno la password. Le righe scadute le toglie lo scheduler.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class LoginAttempt(SQLModel, table=True):
    """Tentativi di accesso falliti di seguito, per (indirizzo, email)."""
    __tablename__ = "login_attempts"
    ip: str = Field(primary_key=True)
    email: str = Field(primary_key=True)
    failures: int = 0
    last_at: datetime  # naive UTC, come le altre colonne


class FlowPresence(SQLModel, table=True):
    """Una scheda dell'editor aperta su un flusso. Nessuna chiave esterna: è una
    riga che vive quaranta secondi, e non deve impedire di eliminare un flusso."""
    __tablename__ = "flow_presence"
    flow_id: int = Field(primary_key=True)
    instance: str = Field(primary_key=True)
    user_id: int
    email: str
    full_name: str = ""
    since: float  # secondi dall'epoca: quando ha aperto, e l'ultimo battito
    seen: float = Field(index=True)
