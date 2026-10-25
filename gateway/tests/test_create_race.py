"""Two creations of the same user or group at once: both pass the «already there?» check,
the unique index stops the second, and the answer is the check's 409, not a 500.

The race is reproduced by making the check see nothing while the row is already there."""
import pytest
from fastapi import HTTPException

from app.models import Group
from app.routes.groups import create_group
from app.routes.users import create_user
from app.schemas.models import GroupCreate, UserCreate
from tests.conftest import make_user


class _Empty:
    def first(self):
        return None


def _check_sees_nothing(session, monkeypatch):
    """The first query of the route (the duplicate check) finds nothing, as when the other
    request has not committed yet; the following ones run as usual."""
    real, calls = session.exec, []

    def exec_(statement, *a, **kw):
        calls.append(statement)
        return _Empty() if len(calls) == 1 else real(statement, *a, **kw)

    monkeypatch.setattr(session, "exec", exec_)


def test_a_user_created_twice_at_once_is_a_409(session, monkeypatch):
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    make_user(session, email="twice@x.local")
    _check_sees_nothing(session, monkeypatch)
    with pytest.raises(HTTPException) as e:
        create_user(body=UserCreate(email="twice@x.local", password="long-enough-1"), session=session, current=boss)
    assert e.value.status_code == 409 and "already registered" in e.value.detail
    # the session is usable again: the failed insert was rolled back
    monkeypatch.undo()
    assert make_user(session, email="after@x.local").id


def test_a_group_created_twice_at_once_is_a_409(session, monkeypatch):
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    session.add(Group(name="twice"))
    session.commit()
    _check_sees_nothing(session, monkeypatch)
    with pytest.raises(HTTPException) as e:
        create_group(body=GroupCreate(name="twice"), session=session, current=boss)
    assert e.value.status_code == 409 and "already a group" in e.value.detail
