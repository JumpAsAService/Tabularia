"""Keeping the database from growing without end (services/housekeeping.py).

Fixed here: the latest contract reports per datasource stay, never one whose notice is
still to be sent; the latest versions of a flow and of a saved script stay, the one in
use always; conversations untouched for long go with their turns, the spending ledger
stays; 0 keeps everything; one pass reports what it touched.
"""
from datetime import datetime, timedelta

from sqlmodel import select

from app.models import (
    AiChat, AiChatTurn, AiSpend, DataContractResult, FlowVersion, SavedScript, SavedScriptVersion,
)
from app.services import housekeeping
from tests.conftest import make_flow, make_project, make_user

NOW = datetime(2026, 10, 22, 12, 0)


def _report(session, ds_id, notify=None):
    session.add(DataContractResult(datasource_id=ds_id, contract_version=1, trigger="refresh", outcome="passed",
                                   report="{}", notify=notify))


def test_the_latest_reports_stay_and_a_notice_to_send_too(session):
    _report(session, 1, notify="pending")  # the oldest: its notice still to send
    for _ in range(5):
        _report(session, 1)
    for _ in range(3):
        _report(session, 2)
    session.commit()
    # datasource 1 keeps its two newest and the pending one; datasource 2 its two newest
    assert housekeeping.contract_results(session, 2) == 3 + 1
    left = session.exec(select(DataContractResult)).all()
    assert sorted(r.datasource_id for r in left) == [1, 1, 1, 2, 2]
    assert sum(1 for r in left if r.notify == "pending") == 1  # never one still to send
    assert housekeeping.contract_results(session, 0) == 0


def test_the_latest_versions_stay_and_the_one_in_use(session):
    flow = make_flow(session, name="f", project_id=make_project(session, name="p").id)
    for v in range(1, 8):
        session.add(FlowVersion(flow_id=flow.id, version=v, definition="{}"))
    script = SavedScript(name="s", project_id=1, environment_id=1, current_version=2)
    session.add(script)
    session.commit()
    for v in range(1, 8):
        session.add(SavedScriptVersion(script_id=script.id, version=v, content="{}"))
    session.commit()
    assert housekeeping.flow_versions(session, 3) == 4
    assert [v.version for v in session.exec(select(FlowVersion).order_by(FlowVersion.version)).all()] == [5, 6, 7]
    assert housekeeping.script_versions(session, 3) == 3  # v1, v3, v4: v2 is the one in use
    assert [v.version for v in session.exec(select(SavedScriptVersion).order_by(SavedScriptVersion.version)).all()] == [2, 5, 6, 7]


def test_old_conversations_go_with_their_turns_the_ledger_stays(session):
    user = make_user(session, email="u@x.local")
    old = AiChat(user_id=user.id, updated_at=NOW - timedelta(days=120))
    recent = AiChat(user_id=user.id, updated_at=NOW - timedelta(days=10))
    session.add_all([old, recent])
    session.commit()
    session.add_all([AiChatTurn(chat_id=old.id, question="q"), AiChatTurn(chat_id=recent.id, question="q"),
                     AiSpend(user_id=user.id)])
    session.commit()
    old_id, recent_id = old.id, recent.id
    assert housekeeping.ai_chats(session, 90, at=NOW) == 1
    session.expire_all()
    assert session.get(AiChat, old_id) is None and session.get(AiChat, recent_id) is not None
    assert [t.chat_id for t in session.exec(select(AiChatTurn)).all()] == [recent_id]
    assert len(session.exec(select(AiSpend)).all()) == 1


def test_one_pass_says_what_it_touched(session):
    for _ in range(3):
        _report(session, 1)
    session.commit()
    done = housekeeping.one_pass(session, at=NOW)
    assert done == {"runs": 0, "data_contract_results": 0, "flow_versions": 0, "saved_script_versions": 0, "ai_chats": 0}


def test_a_kind_that_fails_does_not_stop_the_others(session, monkeypatch):
    for _ in range(3):
        _report(session, 1)
    session.commit()
    monkeypatch.setattr(housekeeping.get_settings().orchestrator, "contract_results_kept", 1)

    def deadlock(*_a, **_k):
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(housekeeping.run_log_retention, "empty_out", deadlock)
    done = housekeeping.one_pass(session, at=NOW)
    assert done["runs"] == 0 and done["data_contract_results"] == 2


def test_a_conversation_opened_again_is_not_taken_away_while_it_answers(session):
    from app.services import ai_chats

    user = make_user(session, email="u@x.local")
    old = AiChat(user_id=user.id, updated_at=datetime.utcnow() - timedelta(days=120))
    session.add(old)
    session.commit()
    ai_chats.open_session(session, user, old.id, "model", None)  # the question arrives on an old conversation
    assert housekeeping.ai_chats(session, 90) == 0
    assert session.get(AiChat, old.id) is not None
