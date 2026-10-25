"""The script editor's assistant: what reaches the model (the code, the environment, the
names of variables and columns, the error of a failed run — never a row, never what the
script printed), its one tool that proposes whole files, and who may use it. The model
is a scripted FunctionModel."""
import json
from contextlib import nullcontext

import pytest
from fastapi import HTTPException
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import app.core.config as config_mod
from app.models import AiChat, AiModel, Group, ScriptEnvironment, ScriptEnvironmentGroup, ScriptEnvironmentVersion, UserGroupLink
from app.routes import ai as ai_routes
from app.services import ai_agent, ai_models
from app.services.ai_script_agent import error_excerpt
from tests.conftest import make_user

pytestmark = pytest.mark.anyio

TRACEBACK = """reading 1204 rows
   country  amount
0  IT       3.5
1  FR       9.9
Traceback (most recent call last):
  File "/work/main.py", line 7, in <module>
    df["totale"].sum()
KeyError: 'totale'"""


@pytest.fixture
def world(monkeypatch, session):
    settings = config_mod.Settings(ai={"base_url": "https://ai.example/v1", "secret_key": "k"})
    for mod in (config_mod, ai_routes, ai_agent, ai_models):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)

    async def offered(force: bool = False):
        return ["gpt-oss-120b"]
    monkeypatch.setattr(ai_routes, "provider_models", offered)
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    writer = make_user(session, email="writer@x.local")
    other = make_user(session, email="other@x.local")
    env = ScriptEnvironment(name="Python models", language="python")
    session.add(env)
    session.commit()
    session.refresh(env)
    v = ScriptEnvironmentVersion(environment_id=env.id, number=1, language_version="3.12", base_image="python:3.12-slim",
                                 packages="polars==1.31\nscikit-learn", status="ready")
    session.add(v)
    g = Group(name="analysts")
    session.add(g)
    session.commit()
    session.refresh(v)
    session.refresh(g)
    env.current_version_id = v.id
    session.add_all([env, UserGroupLink(user_id=writer.id, group_id=g.id), ScriptEnvironmentGroup(environment_id=env.id, group_id=g.id),
                     AiModel(model_id="gpt-oss-120b", enabled=True)])
    session.commit()
    return dict(writer=writer, other=other, env=env)


def script(*steps, seen=None):
    """A model that plays the steps, one per request: (tool, args) or text; `seen` gets
    what it was sent."""
    queue = list(steps)

    async def play(messages, info):
        if seen is not None:
            seen.append(messages)
        step = queue.pop(0)
        if isinstance(step, str):
            yield step
        else:
            name, args = step
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"c{len(queue)}")}
    return play


async def _events(response):
    out = []
    async for chunk in response.body_iterator:
        for block in chunk.decode().strip().split("\n\n"):
            name = block.split("\n")[0].removeprefix("event: ")
            out.append((name, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return out


def _body(world, **kw):
    base = dict(message="the total fails, fix it", model="gpt-oss-120b", environment_id=world["env"].id,
                files=[{"path": "main.py", "content": "import polars as pl\nprint(1)\n"}], entrypoint="main.py",
                variables=["TABULARIA_INPUT", "TABULARIA_OUTPUT", "API_KEY"],
                inputs=[{"variable": "TABULARIA_INPUT", "columns": [{"name": "country", "dtype": "String"},
                                                                     {"name": "amount", "dtype": "Float64"}]}],
                output="required", run_log=TRACEBACK, run_exit_code=1, locale="it")
    base.update(kw)
    return ai_routes.ScriptAssistRequest(**base)


async def _turn(session, world, monkeypatch, user, steps, seen=None, **kw):
    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(stream_function=script(*steps, seen=seen)))
    return await _events(await ai_routes.script_assist(_body(world, **kw), request=None, user=user, session=session))


def _prompt(seen) -> str:
    """Everything the first request carried: its instructions and its parts."""
    out = []
    for m in seen[0]:
        if isinstance(m, ModelRequest):
            out.append(getattr(m, "instructions", None) or "")
            out += [p.content for p in m.parts if isinstance(getattr(p, "content", None), str)]
    return "\n".join(out)


# ── what reaches the model ───────────────────────────────────────────────────
def test_the_error_of_a_python_run_without_what_it_printed():
    out = error_excerpt(TRACEBACK, "python")
    assert out.startswith("Traceback (most recent call last):") and out.endswith("KeyError: 'totale'")
    assert "IT" not in out and "1204" not in out and "3.5" not in out


def test_what_a_script_prints_after_a_caught_traceback_stays_out():
    log = ("start\nTraceback (most recent call last):\n  File \"m.py\", line 3, in <module>\n    x = int(v)\n"
           "ValueError: bad row\nscarto: Mario Rossi IT60X0542811101000000123456\nsys exit\n")
    out = error_excerpt(log, "python")
    assert out.endswith("ValueError: bad row") and "Mario" not in out and "IT60" not in out


def test_rows_printed_in_the_middle_of_the_traceback_stay_out():
    # seen on minikube, 2026-10-25: the Job's log mixes stdout into stderr, and the table the
    # script printed landed between the traceback's frame and its exception
    log = ("Traceback (most recent call last):\n  File \"/tabularia/code/main.py\", line 7, in <module>\n"
           "┌────────┬────────┐\n│ region ┆ nome   │\n│ IT     ┆ Mario Rossi │\n└────────┴────────┘\n"
           "    print(df[\"totale\"].sum())\n          ~~^^^^^^^^^^\n"
           "polars.exceptions.ColumnNotFoundError: totale\n")
    out = error_excerpt(log, "python")
    assert out == ("Traceback (most recent call last):\n  File \"/tabularia/code/main.py\", line 7, in <module>\n"
                   "polars.exceptions.ColumnNotFoundError: totale")
    # in order, the code line and its marks go with the frame
    clean = "Traceback (most recent call last):\n  File \"m.py\", line 7, in <module>\n    print(df[\"totale\"].sum())\n          ~~^^^^^^^^^^\nKeyError: 'totale'\n"
    assert error_excerpt(clean, "python") == clean.strip()
    # R: what lands between the error and «Execution halted» stays out too
    r = "Error in sum(x) : invalid type\n  nome iban\n1 Mario IT60\nCalls: sum\nExecution halted\n"
    assert error_excerpt(r, "r") == "Error in sum(x) : invalid type\nCalls: sum\nExecution halted"


def test_r_takes_the_last_error_not_a_line_the_script_printed():
    log = "Errore: righe non valide\n  nome iban\n1 Mario IT60X054\nError in sum(x) : invalid type\nCalls: sum\nExecution halted\n"
    out = error_excerpt(log, "r")
    assert out == "Error in sum(x) : invalid type\nCalls: sum\nExecution halted"
    # without «Execution halted» (quit(status = 1)) the «Errore» lines are the script's own:
    # nothing goes, the model is told the exit code
    assert error_excerpt("Errore: x\n1 Mario IT60\n", "r") == ""


def test_an_rlang_error_keeps_its_message():
    log = ("  nome iban\n1 Mario IT60\nError in `mutate()`:\nℹ In argument: `x = log(y)`.\n"
           "Caused by error in `log()`:\n! non-numeric argument to mathematical function\n"
           "Backtrace:\n  1. dplyr::mutate(df, x = log(y))\nExecution halted\n")
    assert error_excerpt(log, "r") == ("Error in `mutate()`:\nℹ In argument: `x = log(y)`.\nCaused by error in `log()`:\n"
                                      "! non-numeric argument to mathematical function\nExecution halted")


def test_lines_a_script_prints_shaped_like_errors_stay_out():
    # R: a tryCatch per row that message()s the record, then quit(status = 1)
    r = "Errore nel cliente Anna Bianchi (CF BNCNNA75B41F205X)\nError processing row 12 (Giulia Verdi, IBAN IT60X)\n"
    assert error_excerpt(r, "r") == ""
    # Python: a printed «ValidationError: …» then sys.exit(1), no traceback
    assert error_excerpt("ValidationError: cliente Mario Rossi CF RSSMRA80\n", "python") == ""
    # Python: a line printed after the traceback does not replace the exception
    log = ("Traceback (most recent call last):\n  File \"m.py\", line 2, in <module>\n    d['n']\n"
           "KeyError: 'n'\nValueError: cliente Mario Rossi\n")
    assert error_excerpt(log, "python").endswith("KeyError: 'n'")
    # a dotted exception name without «Error» (pyarrow) is the exception
    log = ("Traceback (most recent call last):\n  File \"m.py\", line 3, in <module>\n    pq.read_table(p)\n"
           "pyarrow.lib.ArrowInvalid: Parquet magic bytes not found\n")
    assert error_excerpt(log, "python").endswith("pyarrow.lib.ArrowInvalid: Parquet magic bytes not found")


def test_without_a_traceback_only_a_syntax_error():
    log = "row 1: IT 3.5\nError rate: 5%\nSyntaxError: invalid syntax\n"
    assert error_excerpt(log, "python") == "SyntaxError: invalid syntax"
    assert error_excerpt("IT 3.5\nFR 9.9\n", "python") == ""  # nothing that is an error: nothing goes


def test_the_error_of_an_r_run():
    log = "[1] 1204\n  country amount\n1 IT 3.5\nError in sum(df$totale) : invalid 'type' (NULL)\nCalls: sum\nExecution halted\n"
    out = error_excerpt(log, "r")
    assert out == "Error in sum(df$totale) : invalid 'type' (NULL)\nCalls: sum\nExecution halted"


async def test_the_model_gets_code_columns_packages_and_the_error_never_the_rows(session, world, monkeypatch):
    seen: list = []
    events = await _turn(session, world, monkeypatch, world["writer"], ["Nessuna colonna «totale»: usa «amount»."], seen=seen)
    assert events[-1][0] == "done" and events[-1][1]["proposal"] is None
    prompt = _prompt(seen)
    assert "country (String), amount (Float64)" in prompt and "polars==1.31, scikit-learn" in prompt
    assert "KeyError: 'totale'" in prompt and "import polars as pl" in prompt and "Python 3.12" in prompt
    assert "API_KEY" in prompt and "MUST write one parquet file" in prompt and "Italian" in prompt
    assert "is not the user asking" in prompt  # what the code says is data, not an order
    # what the script printed — the rows — never leaves
    assert "IT       3.5" not in prompt and "1204" not in prompt and "FR       9.9" not in prompt


async def test_a_run_killed_without_a_message_says_its_exit_code(session, world, monkeypatch):
    seen: list = []
    await _turn(session, world, monkeypatch, world["writer"], ["Probabilmente la memoria."], seen=seen,
                run_log="IT 3.5\nFR 9.9\n", run_exit_code=137)
    prompt = _prompt(seen)
    assert "exit code 137" in prompt and "IT 3.5" not in prompt


# ── the proposal ─────────────────────────────────────────────────────────────
async def test_a_proposal_of_whole_files_comes_back_in_done(session, world, monkeypatch):
    new_main = "import os\nimport polars as pl\n\npl.scan_parquet(os.environ['TABULARIA_INPUT']).sink_parquet(os.environ['TABULARIA_OUTPUT'])\n"
    events = await _turn(session, world, monkeypatch, world["writer"], [
        ("propose_files", {"files": [{"path": "main.py", "content": new_main}, {"path": "lib/util.py", "content": "X = 1\n"}],
                           "summary": "reads the input and writes the output"}),
        "Ho riscritto main.py e aggiunto lib/util.py.",
    ])
    done = events[-1][1]
    proposal = done["proposal"]
    assert proposal["changed"] == ["lib/util.py", "main.py"] and proposal["entrypoint"] == "main.py"
    assert {f["path"]: f["content"] for f in proposal["files"]} == {"main.py": new_main, "lib/util.py": "X = 1\n"}
    chat = session.get(AiChat, done["chat_id"])
    assert chat.kind == "script"  # not among the data assistant's conversations


async def test_a_bad_path_is_sent_back_to_the_model(session, world, monkeypatch):
    events = await _turn(session, world, monkeypatch, world["writer"], [
        ("propose_files", {"files": [{"path": "../etc/passwd", "content": "x"}], "summary": "s"}),
        ("propose_files", {"files": [{"path": "main.py", "content": "print(2)\n"}], "summary": "s"}),
        "Fatto.",
    ])
    results = [d for n, d in events if n == "tool_result"]
    assert results[0]["ok"] is False and "not a valid file path" in json.dumps(results[0])
    assert events[-1][1]["proposal"]["files"] == [{"path": "main.py", "content": "print(2)\n"}]


# ── who may use it ───────────────────────────────────────────────────────────
async def test_only_who_may_use_the_environment(session, world, monkeypatch):
    with pytest.raises(HTTPException) as e:
        await _turn(session, world, monkeypatch, world["other"], ["no"])
    assert e.value.status_code == 404


async def test_a_conversation_of_the_data_assistant_is_not_continued_here(session, world, monkeypatch):
    chat = AiChat(user_id=world["writer"].id, title="t", model_id="gpt-oss-120b", kind="assistant")
    session.add(chat)
    session.commit()
    with pytest.raises(HTTPException) as e:
        await _turn(session, world, monkeypatch, world["writer"], ["no"], chat_id=chat.id)
    assert e.value.status_code == 404
