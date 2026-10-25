"""The route class of every gateway router.

A SYNCHRONOUS route runs in a thread; after it FastAPI validates the answer in
another. `ReleasingRoute` makes the route hand the database connection back before
leaving its own (see `db/session.a_fine_rotta` for why, measured). The asynchronous
routes do not go through a thread per route and stay as they are.

Every gateway `APIRouter` declares it (`route_class=ReleasingRoute`); a test checks
it, because a router that forgets it reopens the stall without anything saying so.
"""
import asyncio

from fastapi.routing import APIRoute

from app.db.session import at_end_of_route


class ReleasingRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        if not asyncio.iscoroutinefunction(endpoint):
            endpoint = at_end_of_route(endpoint)
        super().__init__(path, endpoint, **kwargs)
