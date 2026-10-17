"""A flow's name reaches the Outputs' templates ({flow}: an email subject, a file
name): a control character in it (a line break, a tab) is refused when the flow is
created or renamed. Names saved before are left as they are."""
import pytest
from fastapi import HTTPException

from app.routes.flows import create_flow, update_flow
from app.schemas.models import FlowCreate, FlowUpdate
from tests.conftest import make_flow, make_project, make_user


@pytest.mark.parametrize("name", ["Report\nBcc: x@evil.test", "a\tb", "a\rb", "x\x00"])
def test_create_and_rename_refuse_control_characters(session, name):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    with pytest.raises(HTTPException) as e:
        create_flow(p.id, FlowCreate(name=name), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    flow = create_flow(p.id, FlowCreate(name="Vendite"), request=None, user=admin, session=session)
    with pytest.raises(HTTPException) as e:
        update_flow(flow.id, FlowUpdate(name=name), request=None, user=admin, session=session)
    assert e.value.status_code == 422


def test_an_old_name_is_left_alone_when_other_fields_change(session):
    admin = make_user(session, email="b@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="old\tname", project_id=p.id)
    out = update_flow(flow.id, FlowUpdate(description="d"), request=None, user=admin, session=session)
    assert out.name == "old\tname"
