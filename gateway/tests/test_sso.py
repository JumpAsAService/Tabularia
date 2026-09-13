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


# ── Identità: chi sta entrando ───────────────────────────────────────────────
ISS = "https://idp.example/realms/t"


def claims(email: str, sub: str | None = None, **kw) -> dict:
    """Claim di un id_token realistico. `iss` e `sub` ci sono SEMPRE: li esige
    `validate_id_token`, e sono l'identità su cui l'SSO riconosce l'utente."""
    base = {"iss": ISS, "sub": sub or f"sub-of-{email}", "email": email}
    base.update(kw)
    return base


def test_identita_richiede_iss_e_sub(session):
    """Un token senza identità stabile non identifica nessuno: si rifiuta invece
    di ripiegare sull'email, che è il ripiego che apriva il takeover."""
    for parziale in ({"email": "x@x.local"}, {"iss": ISS, "email": "x@x.local"},
                     {"sub": "s", "email": "x@x.local"}):
        with pytest.raises(sso.SsoError) as e:
            sso.provision_and_sync(session, parziale, cfg())
        assert e.value.code == "no_identity"


def test_claim_booleana_vera_solo_se_lo_dice(session):
    """La vecchia guardia controllava `is False`: claim assente, "false" o 0 la
    superavano. Ora conferma solo un'affermazione esplicita."""
    for vero in (True, "true", "TRUE", "1", "yes"):
        assert sso.claim_is_true(vero) is True
    for falso in (False, "false", "0", "", None, 0, "boh"):
        assert sso.claim_is_true(falso) is False


def test_riconosce_l_utente_dal_subject_anche_se_cambia_email(session):
    """L'email presso l'IdP può cambiare: l'identità no. Stesso account,
    indirizzo aggiornato, nessun duplicato."""
    user, detail = sso.provision_and_sync(session, claims("prima@x.local", sub="s-1"), cfg())
    assert detail["created"] is True

    stesso, detail2 = sso.provision_and_sync(session, claims("dopo@x.local", sub="s-1"), cfg())
    assert stesso.id == user.id and detail2["created"] is False
    assert stesso.email == "dopo@x.local"
    assert len(session.exec(select(User)).all()) == 1


# ── IL TAKEOVER CHE QUESTA MODIFICA CHIUDE ───────────────────────────────────
def test_non_si_entra_in_un_account_esistente_portandone_l_email(session):
    """Il rilievo ALTA dell'audit: un token con l'email dell'admin — ma un
    subject qualsiasi — restituiva l'account dell'admin. Ora serve che l'IdP
    dichiari l'email verificata, e comunque il collegamento avviene una volta sola."""
    admin = make_user(session, email="admin@tabularia.local", is_superuser=True)

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("admin@tabularia.local", sub="attaccante"), cfg())
    assert e.value.code == "email_unverified"

    session.refresh(admin)
    assert admin.oidc_subject is None  # l'account non è stato rivendicato
    assert admin.is_superuser is True


def test_upn_non_verificato_non_rivendica_un_account(session):
    """Stessa difesa sul percorso Entra, dove l'identità arriva da `upn` e la
    claim di verifica tipicamente non c'è affatto."""
    make_user(session, email="vittima@x.local")
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(
            session, {"iss": ISS, "sub": "altro", "upn": "vittima@x.local"}, cfg()
        )
    assert e.value.code == "email_unverified"


def test_collegamento_una_volta_sola_poi_l_identita_e_pinnata(session):
    """Migrazione legittima: un account preesistente si collega al suo utente
    dell'IdP, ma da quel momento nessun'altra identità può reclamarlo."""
    esistente = make_user(session, email="mario@x.local")

    user, detail = sso.provision_and_sync(
        session, claims("mario@x.local", sub="s-mario", email_verified=True), cfg()
    )
    assert user.id == esistente.id
    assert detail["linked"] is True and detail["created"] is False
    assert user.oidc_subject == "s-mario" and user.oidc_issuer == ISS

    # un secondo soggetto con la stessa email ora viene respinto
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(
            session, claims("mario@x.local", sub="s-impostore", email_verified=True), cfg()
        )
    assert e.value.code == "identity_conflict"


def test_il_login_locale_dell_admin_resta_intatto(session):
    """Requisito esplicito: l'SSO non deve togliere all'admin la sua porta."""
    from app.core.security import hash_password, verify_password

    admin = make_user(session, email="admin@tabularia.local",
                      hashed_password=hash_password("segretissima"), is_superuser=True)
    # un tentativo di takeover fallito non tocca la password
    with pytest.raises(sso.SsoError):
        sso.provision_and_sync(session, claims("admin@tabularia.local", sub="x"), cfg())
    session.refresh(admin)
    assert verify_password("segretissima", admin.hashed_password)
    assert admin.is_active and admin.is_superuser


# ── Chi può ENTRARE ──────────────────────────────────────────────────────────
def test_senza_cancelli_entra_chiunque_lidp_autentichi(session):
    """Comportamento storico, invariato: i due cancelli sono spenti di default."""
    user, detail = sso.provision_and_sync(session, claims("tizio@ovunque.example"), cfg())
    assert detail["created"] is True and user.is_active


def test_dominio_email_non_ammesso_non_entra(session):
    c = cfg(allowed_email_domains="azienda.it, altra.it")
    user, _ = sso.provision_and_sync(session, claims("mario@azienda.it"), c)
    assert user.id  # dominio ammesso: entra

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("estraneo@altrove.example"), c)
    assert e.value.code == "not_allowed"
    assert session.exec(select(User).where(User.email == "estraneo@altrove.example")).first() is None


def test_gruppo_obbligatorio_per_accedere(session):
    c = cfg(group_allowlist="analytics", require_allowlisted_group=True)
    make_group(session, "analytics")

    user, _ = sso.provision_and_sync(session, claims("dentro@x.local", groups=["analytics"]), c)
    assert user.id

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("fuori@x.local", groups=["altro"]), c)
    assert e.value.code == "not_allowed"


def test_il_gruppo_admin_basta_per_entrare(session):
    """Chi è nel gruppo che concede l'admin non deve essere elencato due volte."""
    c = cfg(group_allowlist="analytics", require_allowlisted_group=True,
            superuser_group="tabularia-admins")
    user, _ = sso.provision_and_sync(session, claims("capo@x.local", groups=["tabularia-admins"]), c)
    assert user.is_superuser is True


def test_configurazione_contraddittoria_lo_dice(session):
    """Richiedere un gruppo ammesso senza elencarne nessuno bloccherebbe tutti
    in silenzio: meglio un errore esplicito."""
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("x@x.local"), cfg(require_allowlisted_group=True))
    assert e.value.code == "config_error"


# ── Provisioning JIT + sincronizzazione ──────────────────────────────────────
def test_crea_utente_al_primo_login_e_mappa_i_gruppi(session):
    make_group(session, "analytics")
    c = claims("alice@x.local", name="Alice", groups=["/analytics", "sconosciuto"])
    user, detail = sso.provision_and_sync(session, c, cfg())
    assert user.id and user.email == "alice@x.local" and user.full_name == "Alice"
    assert user.hashed_password is None  # utente solo-SSO
    assert user.oidc_subject == "sub-of-alice@x.local"  # identità pinnata subito
    assert detail["created"] is True and detail["groups_added"] == ["analytics"]
    # 'sconosciuto' non esiste in Tabularia e auto_create è spento: ignorato
    assert group_names(session, user) == {"analytics"}


def test_secondo_login_non_duplica_e_completa_il_nome(session):
    make_group(session, "analytics")
    c = claims("alice@x.local", groups=["analytics"])
    user, _ = sso.provision_and_sync(session, c, cfg())
    user2, detail = sso.provision_and_sync(session, {**c, "name": "Alice R."}, cfg())
    assert user2.id == user.id and detail["created"] is False and detail["groups_added"] == []
    assert user2.full_name == "Alice R."
    assert len(session.exec(select(UserGroupLink)).all()) == 1


def test_authoritative_rimuove_i_gruppi_non_piu_nella_claim(session):
    analytics, finance = make_group(session, "analytics"), make_group(session, "finance")
    user = make_user(session, email="bob@x.local")
    for g in (analytics, finance):
        session.add(UserGroupLink(user_id=user.id, group_id=g.id))
    session.commit()

    _, detail = sso.provision_and_sync(
        session, claims("bob@x.local", groups=["analytics"], email_verified=True), cfg()
    )
    assert group_names(session, user) == {"analytics"}
    assert detail["groups_removed"] == ["finance"]


def test_additivo_non_toglie_nulla(session):
    analytics, finance = make_group(session, "analytics"), make_group(session, "finance")
    user = make_user(session, email="bob@x.local")
    session.add(UserGroupLink(user_id=user.id, group_id=finance.id))
    session.commit()

    _, detail = sso.provision_and_sync(
        session, claims("bob@x.local", groups=["analytics"], email_verified=True), cfg(authoritative=False)
    )
    assert group_names(session, user) == {"analytics", "finance"}
    assert detail["groups_removed"] == []


def test_allowlist_filtra_i_gruppi_dell_idp(session):
    make_group(session, "analytics")
    make_group(session, "segreti")
    user, _ = sso.provision_and_sync(
        session, claims("c@x.local", groups=["analytics", "segreti"]), cfg(group_allowlist="analytics"),
    )
    assert group_names(session, user) == {"analytics"}


def test_auto_create_crea_i_gruppi_mancanti(session):
    user, _ = sso.provision_and_sync(
        session, claims("d@x.local", groups=["nuovo-team"]), cfg(auto_create_groups=True),
    )
    assert group_names(session, user) == {"nuovo-team"}
    assert session.exec(select(Group).where(Group.name == "nuovo-team")).first() is not None


def test_superuser_group_concede_e_revoca(session):
    c = cfg(superuser_group="tabularia-admins")
    user, detail = sso.provision_and_sync(
        session, claims("e@x.local", groups=["tabularia-admins"]), c
    )
    assert user.is_superuser is True and detail["superuser_changed"] is True
    # il gruppo admin NON diventa un gruppo di progetto
    assert group_names(session, user) == set()
    user, detail = sso.provision_and_sync(session, claims("e@x.local", groups=[]), c)
    assert user.is_superuser is False and detail["superuser_changed"] is True


def test_utente_disattivato_non_entra_via_sso(session):
    make_user(session, email="f@x.local", is_active=False)
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("f@x.local", groups=[], email_verified=True), cfg())
    assert e.value.code == "user_disabled"


def test_senza_superuser_group_lo_stato_admin_non_viene_toccato(session):
    make_user(session, email="g@x.local", is_superuser=True)
    user, _ = sso.provision_and_sync(
        session, claims("g@x.local", groups=[], email_verified=True), cfg()
    )
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

    user, _ = sso.provision_and_sync(session, claims("solo-sso@x.local", groups=[]), cfg())
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
