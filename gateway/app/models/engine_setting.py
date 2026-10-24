"""Regolazioni del motore impostate dal pannello (Amministrazione → Impostazioni).

Una riga per parametro CAMBIATO: a tabella vuota valgono i valori dell'ambiente,
come prima. È la fonte vera: il gateway le spedisce al motore, che le tiene in
Valkey per i suoi processi, e le rispedisce a intervalli (vedi
services/regolazioni_motore.py). Quali parametri e con che limiti lo decide il
motore (backend/app/core/regolazioni.py).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, Text
from sqlmodel import Field, SQLModel


class EngineSetting(SQLModel, table=True):
    __tablename__ = "engine_settings"

    # "<sezione>.<campo>" delle impostazioni del motore, es. "ingest.parquet_row_group_rows"
    key: str = Field(primary_key=True, max_length=80)
    # interi grandi: il tetto dei byte di BigQuery arriva a decine di TiB
    value: int = Field(sa_column=Column(BigInteger, nullable=False))
    # the value as JSON, for the settings that are not whole numbers (a decimal, true/false):
    # `value` keeps an integer for the previous version of the gateway during an upgrade
    value_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime
    # l'email di chi l'ha cambiato (resta anche se l'utente viene tolto; l'audit ha il resto)
    updated_by: Optional[str] = Field(default=None, max_length=320)
