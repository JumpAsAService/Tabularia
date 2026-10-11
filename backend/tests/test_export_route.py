"""La rotta dell'export passa al motore il tetto delle impostazioni (EXPORT__MAX_ROWS,
audit 2026-10-08) e il rifiuto torna al browser come 400 col motivo."""
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.models import ExportRequest
from app.api.routes import tasks
from app.engine.exceptions import EngineError


def _impostazioni(max_rows):
    return lambda: SimpleNamespace(export=SimpleNamespace(max_rows=max_rows))


@pytest.fixture
def motore(monkeypatch):
    visto = {}

    class Finto:
        def export(self, **kw):
            visto.update(kw)
            if visto.get("rifiuta"):
                raise EngineError("il risultato ha più di 7 righe: oltre il tetto dell'export CSV.")
            with open(kw["out_path"], "w") as f:
                f.write("a\n1\n")

    monkeypatch.setattr(tasks, "get_engine", lambda nome: Finto())
    return visto


@pytest.mark.parametrize("impostato, passato", [(7, 7), (0, None)])
def test_la_rotta_passa_il_tetto_delle_impostazioni(motore, monkeypatch, impostato, passato):
    monkeypatch.setattr(tasks, "get_settings", _impostazioni(impostato))
    risposta = tasks.export_flow(ExportRequest(bucket="b", input_key="k", format="csv"))
    try:
        assert motore["max_rows"] == passato and motore["fmt"] == "csv"
    finally:
        os.unlink(risposta.path)


def test_oltre_il_tetto_e_un_400_col_motivo_e_niente_file(motore, monkeypatch):
    monkeypatch.setattr(tasks, "get_settings", _impostazioni(7))
    motore["rifiuta"] = True
    with pytest.raises(HTTPException) as e:
        tasks.export_flow(ExportRequest(bucket="b", input_key="k", format="csv"))
    assert e.value.status_code == 400 and "tetto dell'export CSV" in e.value.detail
    assert not os.path.exists(motore["out_path"])
