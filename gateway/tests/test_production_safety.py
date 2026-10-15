"""In produzione il gateway rifiuta di partire con segreti deboli (audit 2026-10-08, B3).

Si rifiutava solo il default di sviluppo del segreto JWT: qualunque altro valore,
anche di un carattere, passava. Il segreto firma ogni sessione con HS256: uno
corto si indovina offline partendo da un solo token, e chi lo indovina si firma
un token da amministratore."""
import pytest

from app.core.config import JWT_SECRET_MIN, Settings

BUONO = "a" * 64


def _produzione(**kw):
    base = {"app": {"env_name": "production"}, "jwt": {"secret": BUONO},
            "auth": {"admin_password": "una-password-vera"}, "db": {"password": "un-altra-vera"}}
    base.update(kw)
    return Settings(**base)


def test_segreti_veri_passano():
    _produzione().check_production_safety()


@pytest.mark.parametrize("segreto, motivo", [
    ("change-me-in-production", "development default"),
    ("a", "too short (1 characters"),
    ("x" * (JWT_SECRET_MIN - 1), f"too short ({JWT_SECRET_MIN - 1} characters"),
])
def test_un_segreto_jwt_debole_ferma_la_partenza(segreto, motivo):
    with pytest.raises(RuntimeError, match="JWT__SECRET") as e:
        _produzione(jwt={"secret": segreto}).check_production_safety()
    assert motivo in str(e.value)


def test_al_minimo_passa():
    _produzione(jwt={"secret": "x" * JWT_SECRET_MIN}).check_production_safety()


def test_in_sviluppo_non_si_controlla():
    Settings(app={"env_name": "development"}, jwt={"secret": "a"}).check_production_safety()
