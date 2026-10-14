"""Permission answers are reused within one request (review of 2026-10-14: a preview by
a non-admin reloaded the whole folder tree and every grant of every user several
times, 0.25 s at 2,000 folders). They must never outlive a write, a commit or the
request, and sessions outside a request keep asking the database every time."""
from sqlalchemy import event

from app.models import Group, Permission, Project, UserGroupLink
from app.services import permissions
from tests.conftest import make_permission, make_project, make_user


def _statements(engine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(conn, cursor, statement, *_):
        seen.append(statement)

    return seen


def _scene(session):
    anna = make_user(session, email="anna@x.local")
    other = make_user(session, email="other@x.local")
    sales = make_project(session, name="Sales").id
    north = make_project(session, name="North", parent_id=sales).id
    finance = make_project(session, name="Finance").id
    make_permission(session, user_id=anna.id, project_id=sales, capability="view")
    for _ in range(3):  # other people's grants are none of anna's business
        make_permission(session, user_id=other.id, project_id=finance, capability="edit")
    # ids, not rows: after a commit reading a row's id would load it again
    return anna, sales, north, finance


def _as_request(session, user):
    session.commit()
    user.id  # noqa: B018 — loaded now, as the request's authentication leaves it
    session.info[permissions.REQUEST_SESSION] = True


def test_one_request_reads_the_tree_and_the_grants_once(db_engine, session):
    anna, sales, north, finance = _scene(session)
    _as_request(session, anna)
    seen = _statements(db_engine)

    # what a preview, a search and a folder page ask, one after the other
    for _ in range(3):
        assert not permissions.is_admin(session, anna)
    assert permissions.readable_project_ids(session, anna) == {sales, north}
    assert permissions.visible_project_ids(session, anna) == {sales, north}
    assert permissions.connectable_project_ids(session, anna) == set()
    assert permissions.has_capability(session, anna, north, "view")
    assert not permissions.has_capability(session, anna, finance, "view")
    assert not permissions.can_upload(session, anna)

    assert sum("FROM projects" in s for s in seen) == 1
    grants = [s for s in seen if "FROM permissions" in s]
    assert len(grants) == 1 and "permissions.user_id = " in grants[0]  # only the subject's grants
    assert sum("FROM user_groups" in s or "JOIN user_groups" in s for s in seen) <= 2  # groups, admin groups


def test_a_write_in_the_same_request_is_seen_at_once(session):
    anna, sales, north, finance = _scene(session)
    _as_request(session, anna)
    assert not permissions.has_capability(session, anna, finance, "view")
    session.add(Permission(project_id=finance, user_id=anna.id, capability="view"))
    session.flush()
    assert permissions.has_capability(session, anna, finance, "view")

    # a new folder under a readable one, and a group that makes anna an admin
    child = Project(name="Child", parent_id=finance)
    session.add(child)
    session.flush()
    assert child.id in permissions.readable_project_ids(session, anna)
    admins = Group(name="admins", is_admin=True)
    session.add(admins)
    session.flush()
    session.add(UserGroupLink(user_id=anna.id, group_id=admins.id))
    session.flush()
    assert permissions.is_admin(session, anna)


def test_a_commit_forgets_and_a_later_change_is_seen(db_engine, session):
    anna, sales, north, finance = _scene(session)
    _as_request(session, anna)
    assert permissions.readable_project_ids(session, anna) == {sales, north}
    session.commit()  # e.g. the proxy releases the database before waiting for the engine
    with session.get_bind().connect() as other:  # someone else revokes the grant meanwhile
        other.exec_driver_sql("DELETE FROM permissions WHERE user_id = ?", (anna.id,))
        other.commit()
    assert permissions.readable_project_ids(session, anna) == set()


def test_outside_a_request_every_question_reads_again(db_engine, session):
    anna, *_ = _scene(session)
    seen = _statements(db_engine)
    permissions.readable_project_ids(session, anna)
    permissions.readable_project_ids(session, anna)
    assert sum("FROM projects" in s for s in seen) == 2


def test_changing_an_answer_does_not_change_the_next_one(session):
    anna, sales, north, _ = _scene(session)
    _as_request(session, anna)
    mine = permissions.readable_project_ids(session, anna)
    mine.add(999)
    groups = permissions.user_group_ids(session, anna)
    groups.add(999)
    assert permissions.readable_project_ids(session, anna) == {sales, north}
    assert permissions.user_group_ids(session, anna) == set()
