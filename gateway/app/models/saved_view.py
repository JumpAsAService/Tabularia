"""Viste salvate: una configurazione NOMINATA del Viewer, dentro una cartella.

Il Viewer costruisce al volo filtri, campi calcolati e pivot su una datasource e
non salva nulla. Una vista salvata è quella stessa configurazione messa nel
catalogo accanto a flussi, datasource e connessioni — quindi con nome, cartella
e permessi ereditati dal progetto, come tutto il resto.

Due proprietà scelte esplicitamente:

- **la vista SEGUE il dato, non lo congela.** Si memorizza un riferimento alla
  datasource e la configurazione, mai delle righe: riaprirla domani rilegge lo
  snapshot corrente. È una lente, non una copia — e per questo non ha alcun
  blob, nessuno snapshot da sostituire, niente da raccogliere per `blobgc`.
- **è condivisa, non personale.** Sta in una cartella, quindi la vede chiunque
  abbia VIEW su quella cartella; modificarla o cancellarla richiede EDIT, come
  per i flussi. `owner_id` dice chi l'ha creata, non chi può vederla.

NB sul nome: nel codice resta `SavedView`, mai `View` — `VIEW` è già il nome di
una capability (`Capability.VIEW`) e confonderli nei permessi sarebbe un invito
all'errore. Nell'interfaccia si chiama semplicemente «vista».
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SavedView(SQLModel, table=True):
    __tablename__ = "saved_views"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_saved_view_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    # la datasource su cui la vista si applica. Nessuna cancellazione a cascata:
    # se la datasource sparisce la vista resta e lo dice aprendola, invece di
    # svanire in silenzio dalla cartella di qualcun altro.
    datasource_id: int = Field(foreign_key="datasources.id", index=True)

    # Configurazione del Viewer in JSON: engine, filtri, campi calcolati, pivot.
    # Testo opaco per il gateway — è il FRONTEND a possederne la forma, e il
    # gateway si limita a verificare che sia JSON valido. Così aggiungere un
    # controllo alla vista non richiede una migrazione qui.
    spec: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
