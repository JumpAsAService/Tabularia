"""SSO OIDC opzionale: configurazione, provisioning JIT e sincronizzazione gruppi.

Copre le due cuciture del design (docs/design/sso-group-mapping.md):
- **accettazione**: transazione di login firmata (state/nonce/PKCE), rifiuto di
  state non corrispondente, token scaduto o di tipo errato;
- **sincronizzazione**: la claim dell'IdP riconciliata su `user_groups`, che è
  l'unica cosa letta dall'RBAC.

Niente rete: discovery e id_token sono sostituiti da fixture. Il flusso completo
contro un IdP vero sta in docs/examples/keycloak/.
"""
import time

import pytest
from sqlmodel import select

from app.core.config import OidcSettings, Settings
from app.models import Group, User, UserGroupLink
from app.services import sso
from app.services.permissions import user_group_ids
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _sso_isolato_dall_ambiente(monkeypatch):
    """I test partono SEMPRE con l'SSO spento, qualunque sia l'ambiente del
    container: un `.env` di prova con OIDC__* non deve cambiarne l'esito."""
    import os

    from app.core.config import get_settings

    for key in [k for k in os.environ if k.startswith("OIDC__")]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    sso.reset_discovery_cache()
    yield
    get_settings.cache_clear()


def cfg(**kw) -> OidcSettings:
    base = dict(issuer="https://idp.example/realms/t", client_id="tabularia-gateway")
    base.update(kw)
    return OidcSettings(**base)


def make_group(session, name: str) -> Group:
    g = Group(name=name)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def group_names(session, user: User) -> set[str]:
    gids = user_group_ids(session, user)
    return set(session.exec(select(Group.name).where(Group.id.in_(gids))).all()) if gids else set()


# ── Configurazione: opzionale e fail-fast se incompleta ──────────────────────
def test_sso_spento_di_default():
    s = Settings()
    assert s.oidc.enabled is False
    s.check_sso_config()  # nessun vincolo quando è spento
    assert sso.public_config()["enabled"] in (True, False)  # non solleva mai


def test_enabled_richiede_issuer_e_client_id():
    assert OidcSettings(issuer="https://idp", client_id="").enabled is False
    assert OidcSettings(issuer="", client_id="abc").enabled is False
    assert cfg().enabled is True


def test_configurazione_incompleta_blocca_lo_startup():
    s = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c"})
    with pytest.raises(RuntimeError) as e:
        s.check_sso_config()
    msg = str(e.value)
    assert "OIDC__CLIENT_SECRET" in msg and "OIDC__REDIRECT_URI" in msg


def test_configurazione_completa_passa_e_scopes_richiede_openid():
    full = {"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
            "redirect_uri": "https://gw/auth/sso/callback"}
    Settings(oidc=full).check_sso_config()
    with pytest.raises(RuntimeError, match="openid"):
        Settings(oidc={**full, "scopes": "profile email"}).check_sso_config()


def test_allowlist_e_scopes_si_leggono_da_stringa():
    c = cfg(group_allowlist=" analytics , finance ,, ", scopes="openid profile")
    assert c.allowlist == {"analytics", "finance"}
    assert c.scope_list == ["openid", "profile"]
    assert c.discovery_url == "https://idp.example/realms/t/.well-known/openid-configuration"


# ── Transazione di login: state, nonce, PKCE ─────────────────────────────────
def test_pkce_challenge_deriva_dal_verifier():
    import base64, hashlib

    verifier, challenge = sso.new_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in challenge


def test_tx_token_roundtrip_e_state_sbagliato():
    token = sso.issue_tx_token("st4te", "n0nce", "verifier")
    tx = sso.read_tx_token(token, "st4te")
    assert tx["nonce"] == "n0nce" and tx["cv"] == "verifier"
    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(token, "altro-state")
    assert e.value.code == "state_mismatch"


def test_tx_token_scaduto_o_di_tipo_errato():
    import jwt as pyjwt

    from app.core.config import get_settings

    secret = get_settings().jwt.secret.get_secret_value()
    scaduto = pyjwt.encode({"typ": sso.TX_TOKEN_TYPE, "state": "s", "exp": int(time.time()) - 10}, secret, algorithm="HS256")
    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(scaduto, "s")
    assert e.value.code == "tx_expired"
    # un token di sessione normale non vale come transazione di login
    from app.core.security import create_access_token

    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(create_access_token(1), "s")
    assert e.value.code == "tx_invalid"


# ── Claim → identità ─────────────────────────────────────────────────────────
def test_email_dalle_claim_con_fallback_entra():
    assert sso.email_from_claims({"email": "A@X.local"}) == "a@x.local"
    assert sso.email_from_claims({"preferred_username": "b@x.local"}) == "b@x.local"
    assert sso.email_from_claims({"upn": "c@x.local"}) == "c@x.local"
    # preferred_username non-email (Keycloak manda lo username) → si ignora
    assert sso.email_from_claims({"preferred_username": "alice", "email": "d@x.local"}) == "d@x.local"
    with pytest.raises(sso.SsoError) as e:
        sso.email_from_claims({"preferred_username": "alice"})
    assert e.value.code == "no_email"


def test_normalize_groups_keycloak_ed_entra():
    c = cfg()
    assert sso.normalize_groups({"groups": ["/analytics", "finance "]}, c) == {"analytics", "finance"}
    assert sso.normalize_groups({"groups": "solo-uno"}, c) == {"solo-uno"}
    assert sso.normalize_groups({}, c) == set()
    assert sso.normalize_groups({"groups": None}, c) == set()
    assert sso.normalize_groups({"groups": {"non": "una lista"}}, c) == set()
    # Entra: app role su claim `roles`
    entra = cfg(groups_claim="roles")
    assert sso.normalize_groups({"roles": ["Analytics.Read"], "groups": ["ignorato"]}, entra) == {"Analytics.Read"}


# ── Provisioning JIT + sincronizzazione ──────────────────────────────────────
def test_crea_utente_al_primo_login_e_mappa_i_gruppi(session):
    make_group(session, "analytics")
    claims = {"email": "alice@x.local", "name": "Alice", "groups": ["/analytics", "sconosciuto"]}
    user, detail = sso.provision_and_sync(session, claims, cfg())
    assert user.id and user.email == "alice@x.local" and user.full_name == "Alice"
    assert user.hashed_password is None  # utente solo-SSO
    assert detail["created"] is True and detail["groups_added"] == ["analytics"]
    # 'sconosciuto' non esiste in Tabularia e auto_create è spento: ignorato
    assert group_names(session, user) == {"analytics"}


def test_secondo_login_non_duplica_e_completa_il_nome(session):
    make_group(session, "analytics")
    claims = {"email": "alice@x.local", "groups": ["analytics"]}
    user, _ = sso.provision_and_sync(session, claims, cfg())
    user2, detail = sso.provision_and_sync(session, {**claims, "name": "Alice R."}, cfg())
    assert user2.id == user.id and detail["created"] is False and detail["groups_added"] == []
    assert user2.full_name == "Alice R."
    assert len(session.exec(select(UserGroupLink)).all()) == 1


def test_authoritative_rimuove_i_gruppi_non_piu_nella_claim(session):
    analytics, finance = make_group(session, "analytics"), make_group(session, "finance")
    user = make_user(session, email="bob@x.local")
    for g in (analytics, finance):
        session.add(UserGroupLink(user_id=user.id, group_id=g.id))
    session.commit()

    _, detail = sso.provision_and_sync(session, {"email": "bob@x.local", "groups": ["analytics"]}, cfg())
    assert group_names(session, user) == {"analytics"}
    assert detail["groups_removed"] == ["finance"]


def test_additivo_non_toglie_nulla(session):
    analytics, finance = make_group(session, "analytics"), make_group(session, "finance")
    user = make_user(session, email="bob@x.local")
    session.add(UserGroupLink(user_id=user.id, group_id=finance.id))
    session.commit()

    _, detail = sso.provision_and_sync(session, {"email": "bob@x.local", "groups": ["analytics"]}, cfg(authoritative=False))
    assert group_names(session, user) == {"analytics", "finance"}
    assert detail["groups_removed"] == []


def test_allowlist_filtra_i_gruppi_dell_idp(session):
    make_group(session, "analytics")
    make_group(session, "segreti")
    user, _ = sso.provision_and_sync(
        session, {"email": "c@x.local", "groups": ["analytics", "segreti"]}, cfg(group_allowlist="analytics"),
    )
    assert group_names(session, user) == {"analytics"}


def test_auto_create_crea_i_gruppi_mancanti(session):
    user, _ = sso.provision_and_sync(
        session, {"email": "d@x.local", "groups": ["nuovo-team"]}, cfg(auto_create_groups=True),
    )
    assert group_names(session, user) == {"nuovo-team"}
    assert session.exec(select(Group).where(Group.name == "nuovo-team")).first() is not None


def test_superuser_group_concede_e_revoca(session):
    c = cfg(superuser_group="tabularia-admins")
    user, detail = sso.provision_and_sync(session, {"email": "e@x.local", "groups": ["tabularia-admins"]}, c)
    assert user.is_superuser is True and detail["superuser_changed"] is True
    # il gruppo admin NON diventa un gruppo di progetto
    assert group_names(session, user) == set()
    user, detail = sso.provision_and_sync(session, {"email": "e@x.local", "groups": []}, c)
    assert user.is_superuser is False and detail["superuser_changed"] is True


def test_utente_disattivato_non_entra_via_sso(session):
    make_user(session, email="f@x.local", is_active=False)
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, {"email": "f@x.local", "groups": []}, cfg())
    assert e.value.code == "user_disabled"


def test_senza_superuser_group_lo_stato_admin_non_viene_toccato(session):
    make_user(session, email="g@x.local", is_superuser=True)
    user, _ = sso.provision_and_sync(session, {"email": "g@x.local", "groups": []}, cfg())
    assert user.is_superuser is True  # gestito a mano dall'admin, l'SSO non decide


# ── Rotte: comportamento quando l'SSO è spento o il flusso è manomesso ───────
class FakeRequest:
    """Minimo indispensabile per le rotte: cookie, header e client (per l'audit)."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}
        self.headers = {}
        self.client = type("C", (), {"host": "127.0.0.1"})()


def test_config_pubblica_non_espone_nulla_di_sensibile(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: Settings(oidc={
        "issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "segretissimo",
        "button_label": "Entra con Keycloak",
    }))
    monkeypatch.setattr(sso, "get_settings", config_mod.get_settings)
    pub = sso.public_config()
    assert pub == {"enabled": True, "button_label": "Entra con Keycloak"}
    assert "segretissimo" not in str(pub) and "issuer" not in pub


def test_login_e_callback_con_sso_spento_non_esplodono(session):
    from app.routes import sso as sso_routes

    res = sso_routes.sso_login(FakeRequest())
    assert res.status_code == 303 and "error=sso_disabled" in res.headers["location"]
    res = sso_routes.sso_callback(FakeRequest(), code="x", state="y", session=session)
    assert res.status_code == 303 and "error=sso_disabled" in res.headers["location"]


def test_callback_senza_cookie_di_transazione_viene_respinto(session, monkeypatch):
    from app.core import config as config_mod
    from app.routes import sso as sso_routes

    enabled = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c",
                             "client_secret": "s", "redirect_uri": "https://gw/auth/sso/callback"})
    monkeypatch.setattr(sso_routes, "get_settings", lambda: enabled)
    monkeypatch.setattr(config_mod, "get_settings", lambda: enabled)
    res = sso_routes.sso_callback(FakeRequest(), code="abc", state="s", session=session)
    assert res.status_code == 303 and "error=tx_missing" in res.headers["location"]
    # l'IdP che rifiuta il login non deve mai propagare il suo messaggio all'utente
    res = sso_routes.sso_callback(FakeRequest(), error="access_denied",
                                  error_description="consent required", session=session)
    assert "error=idp_denied" in res.headers["location"]
    assert "consent" not in res.headers["location"]


def test_il_login_locale_rifiuta_un_utente_solo_sso(session):
    """Utente creato dall'SSO (senza password locale): niente back-door con
    password vuota. Il login locale resta per gli utenti che ne hanno una."""
    from app.routes.auth import login
    from app.schemas.models import LoginRequest
    from fastapi import HTTPException

    user, _ = sso.provision_and_sync(session, {"email": "solo-sso@x.local", "groups": []}, cfg())
    assert user.hashed_password is None
    for password in ("", "x", "qualsiasi"):
        with pytest.raises(HTTPException) as e:
            login(LoginRequest(email="solo-sso@x.local", password=password), FakeRequest(), session)
        assert e.value.status_code == 401


def test_callback_ok_emette_il_token_interno_e_scrive_l_audit(session, monkeypatch):
    """Percorso felice della rotta: token INTERNO nel frammento, cookie di
    transazione cancellato, evento di audit col delta dei gruppi."""
    from app.core import config as config_mod
    from app.core.security import decode_access_token
    from app.models import AuditLog
    from app.routes import sso as sso_routes

    enabled = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
                             "redirect_uri": "https://gw/auth/sso/callback",
                             "post_login_url": "https://app.example/auth/callback"})
    monkeypatch.setattr(sso_routes, "get_settings", lambda: enabled)
    monkeypatch.setattr(config_mod, "get_settings", lambda: enabled)

    user = make_user(session, email="h@x.local")
    monkeypatch.setattr(sso_routes.sso, "login_user",
                        lambda s, code, tx, cfg: (user, {"created": False, "groups_added": ["analytics"]}))

    tx_cookie = sso.issue_tx_token("s1", "n1", "v1")
    res = sso_routes.sso_callback(FakeRequest({sso.TX_COOKIE: tx_cookie}), code="ok", state="s1", session=session)

    assert res.status_code == 303
    location = res.headers["location"]
    assert location.startswith("https://app.example/auth/callback#token=")
    # è il TOKEN INTERNO di Tabularia, identico a quello del login locale
    assert int(decode_access_token(location.split("#token=")[1])["sub"]) == user.id
    # il cookie di transazione è a uso singolo
    assert 'tab_sso_tx=""' in res.headers.get("set-cookie", "") or "Max-Age=0" in res.headers.get("set-cookie", "")

    entry = session.exec(select(AuditLog).where(AuditLog.action == "auth.sso_login")).first()
    assert entry is not None and entry.actor_id == user.id and "analytics" in (entry.detail or "")
