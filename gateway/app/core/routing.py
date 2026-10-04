"""La classe di rotta di ogni router del gateway.

Una rotta SINCRONA gira in un thread; dopo di lei FastAPI valida la risposta in
un altro. `RottaCheRilascia` fa sì che la rotta renda la connessione al database
prima di uscire dal suo (vedi `db/session.a_fine_rotta` per il perché, misurato).
Le rotte asincrone non passano da un thread per rotta e restano come sono.

Ogni `APIRouter` del gateway la dichiara (`route_class=RottaCheRilascia`); un
test lo verifica, perché un router che se ne dimentica riapre lo stallo senza
che niente lo dica.
"""
import asyncio

from fastapi.routing import APIRoute

from app.db.session import a_fine_rotta


class RottaCheRilascia(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        if not asyncio.iscoroutinefunction(endpoint):
            endpoint = a_fine_rotta(endpoint)
        super().__init__(path, endpoint, **kwargs)
