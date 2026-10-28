"""Optional OIDC SSO: configuration, JIT provisioning and group synchronization.

Covers the two seams of the design (docs/design/sso-group-mapping.md):
- **acceptance**: signed login transaction (state/nonce/PKCE), refusal of a state that
  does not match, an expired token or one of the wrong type;
- **synchronization**: the IdP's claim reconciled onto `user_groups`, the only thing
  RBAC reads.

No network: discovery and id_token are replaced by fixtures. The whole flow against a
real IdP is in docs/examples/keycloak/.
"""
import ssl
import time

import pytest
from sqlmodel import select

from app.core.config import OidcSettings, Settings
from app.models import Group, User, UserGroupLink
from app.services import sso
from app.services.permissions import user_group_ids
from tests.conftest import make_user


@pytest.fixture(autouse=True)
def _sso_isolated_from_environment(monkeypatch):
    """The tests ALWAYS start with SSO off, whatever the container's environment: a test
    `.env` with OIDC__* must not change their outcome."""
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


# ── Configuration: optional, and fail-fast when incomplete ───────────────────
def test_sso_off_by_default():
    s = Settings()
    assert s.oidc.enabled is False
    s.check_sso_config()  # no constraint while off
    assert sso.public_config()["enabled"] in (True, False)  # never raises


def test_enabled_requires_issuer_and_client_id():
    assert OidcSettings(issuer="https://idp", client_id="").enabled is False
    assert OidcSettings(issuer="", client_id="abc").enabled is False
    assert cfg().enabled is True


def test_incomplete_configuration_blocks_the_startup():
    s = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c"})
    with pytest.raises(RuntimeError) as e:
        s.check_sso_config()
    msg = str(e.value)
    assert "OIDC__CLIENT_SECRET" in msg and "OIDC__REDIRECT_URI" in msg


def test_complete_configuration_passes_and_scopes_requires_openid():
    full = {"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
            "redirect_uri": "https://gw/auth/sso/callback"}
    Settings(oidc=full).check_sso_config()
    with pytest.raises(RuntimeError, match="openid"):
        Settings(oidc={**full, "scopes": "profile email"}).check_sso_config()


def test_allowlist_and_scopes_are_read_from_a_string():
    c = cfg(group_allowlist=" analytics , finance ,, ", scopes="openid profile")
    assert c.allowlist == {"analytics", "finance"}
    assert c.scope_list == ["openid", "profile"]
    assert c.discovery_url == "https://idp.example/realms/t/.well-known/openid-configuration"


# ── Login transaction: state, nonce, PKCE ────────────────────────────────────
def test_pkce_challenge_derives_from_the_verifier():
    import base64, hashlib

    verifier, challenge = sso.new_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in challenge


def test_tx_token_roundtrip_and_wrong_state():
    token = sso.issue_tx_token("st4te", "n0nce", "verifier")
    tx = sso.read_tx_token(token, "st4te")
    assert tx["nonce"] == "n0nce" and tx["cv"] == "verifier"
    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(token, "altro-state")
    assert e.value.code == "state_mismatch"


def test_tx_token_expired_or_of_the_wrong_type():
    import jwt as pyjwt

    from app.core.config import get_settings

    secret = get_settings().jwt.secret.get_secret_value()
    # expired beyond the clocks' tolerance (a minute)
    expired = pyjwt.encode({"typ": sso.TX_TOKEN_TYPE, "state": "s", "exp": int(time.time()) - 120}, secret, algorithm="HS256")
    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(expired, "s")
    assert e.value.code == "tx_expired"
    # a normal session token does not hold as a login transaction
    from app.core.security import create_access_token

    with pytest.raises(sso.SsoError) as e:
        sso.read_tx_token(create_access_token(1, "sid"), "s")
    assert e.value.code == "tx_invalid"


# ── Claim → identity ─────────────────────────────────────────────────────────
def test_email_from_the_claims_with_fallback_upn():
    assert sso.email_from_claims({"email": "A@X.local"}) == "a@x.local"
    assert sso.email_from_claims({"preferred_username": "b@x.local"}) == "b@x.local"
    assert sso.email_from_claims({"upn": "c@x.local"}) == "c@x.local"
    # a non-email preferred_username (Keycloak sends the username) → ignored
    assert sso.email_from_claims({"preferred_username": "alice", "email": "d@x.local"}) == "d@x.local"
    with pytest.raises(sso.SsoError) as e:
        sso.email_from_claims({"preferred_username": "alice"})
    assert e.value.code == "no_email"


def test_normalize_groups_keycloak_and_entra():
    c = cfg()
    assert sso.normalize_groups({"groups": ["/analytics", "finance "]}, c) == {"analytics", "finance"}
    assert sso.normalize_groups({"groups": "solo-uno"}, c) == {"solo-uno"}
    assert sso.normalize_groups({}, c) == set()
    assert sso.normalize_groups({"groups": None}, c) == set()
    assert sso.normalize_groups({"groups": {"non": "una lista"}}, c) == set()
    # Entra: app roles on the `roles` claim
    enters = cfg(groups_claim="roles")
    assert sso.normalize_groups({"roles": ["Analytics.Read"], "groups": ["ignorato"]}, enters) == {"Analytics.Read"}


# ── Identity: who is signing in ──────────────────────────────────────────────
ISS = "https://idp.example/realms/t"


def claims(email: str, sub: str | None = None, **kw) -> dict:
    """The claims of a realistic id_token. `iss` and `sub` are ALWAYS there:
    `validate_id_token` requires them, and they are the identity SSO recognizes the user
    by."""
    base = {"iss": ISS, "sub": sub or f"sub-of-{email}", "email": email}
    base.update(kw)
    return base


def test_identity_requires_iss_and_sub(session):
    """A token without a stable identity identifies nobody: it is refused instead of falling
    back on the email, which is the fallback that opened the takeover."""
    for partial in ({"email": "x@x.local"}, {"iss": ISS, "email": "x@x.local"},
                     {"sub": "s", "email": "x@x.local"}):
        with pytest.raises(sso.SsoError) as e:
            sso.provision_and_sync(session, partial, cfg())
        assert e.value.code == "no_identity"


def test_boolean_claim_true_only_when_it_says_so(session):
    """The old guard checked `is False`: a missing claim, "false" or 0 got past it. Now only
    an explicit statement confirms."""
    for real in (True, "true", "TRUE", "1", "yes"):
        assert sso.claim_is_true(real) is True
    for false_ in (False, "false", "0", "", None, 0, "boh"):
        assert sso.claim_is_true(false_) is False


def test_recognizes_the_user_by_subject_even_when_the_email_changes(session):
    """The email at the IdP may change: the identity does not. The same account, the address
    updated, no duplicate."""
    user, detail = sso.provision_and_sync(session, claims("prima@x.local", sub="s-1"), cfg())
    assert detail["created"] is True

    same, detail2 = sso.provision_and_sync(session, claims("dopo@x.local", sub="s-1"), cfg())
    assert same.id == user.id and detail2["created"] is False
    assert same.email == "dopo@x.local"
    assert len(session.exec(select(User)).all()) == 1


# ── THE TAKEOVER THIS CHANGE CLOSES ──────────────────────────────────────────
def test_an_existing_account_cannot_be_entered_by_carrying_its_email(session):
    """The audit's HIGH finding: a token with the admin's email — but any subject — returned
    the admin's account. Now the IdP must declare the email verified, and the link happens
    once only anyway."""
    admin = make_user(session, email="admin@tabularia.local", is_superuser=True)

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("admin@tabularia.local", sub="attaccante"), cfg())
    assert e.value.code == "email_unverified"

    session.refresh(admin)
    assert admin.oidc_subject is None  # the account was not claimed
    assert admin.is_superuser is True


def test_unverified_upn_does_not_claim_an_account(session):
    """The same defence on the Entra path, where the identity comes from `upn` and the
    verification claim is typically not there at all."""
    make_user(session, email="vittima@x.local")
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(
            session, {"iss": ISS, "sub": "altro", "upn": "vittima@x.local"}, cfg()
        )
    assert e.value.code == "email_unverified"


def test_linked_once_only_then_the_identity_is_pinned(session):
    """A legitimate migration: a pre-existing account links to its IdP user, but from then on
    no other identity can claim it."""
    existing = make_user(session, email="mario@x.local")

    user, detail = sso.provision_and_sync(
        session, claims("mario@x.local", sub="s-mario", email_verified=True), cfg()
    )
    assert user.id == existing.id
    assert detail["linked"] is True and detail["created"] is False
    assert user.oidc_subject == "s-mario" and user.oidc_issuer == ISS

    # a second subject with the same email is now refused
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(
            session, claims("mario@x.local", sub="s-impostore", email_verified=True), cfg()
        )
    assert e.value.code == "identity_conflict"


def test_the_admins_local_login_stays_intact(session):
    """An explicit requirement: SSO must not take the admin's door away."""
    from app.core.security import hash_password, verify_password

    admin = make_user(session, email="admin@tabularia.local",
                      hashed_password=hash_password("segretissima"), is_superuser=True)
    # a failed takeover attempt does not touch the password
    with pytest.raises(sso.SsoError):
        sso.provision_and_sync(session, claims("admin@tabularia.local", sub="x"), cfg())
    session.refresh(admin)
    assert verify_password("segretissima", admin.hashed_password)
    assert admin.is_active and admin.is_superuser


# ── Who may SIGN IN ──────────────────────────────────────────────────────────
def test_without_gates_anyone_the_idp_authenticates_signs_in(session):
    """The historical behaviour, unchanged: the two gates are off by default."""
    user, detail = sso.provision_and_sync(session, claims("tizio@ovunque.example"), cfg())
    assert detail["created"] is True and user.is_active


def test_email_domain_not_allowed_does_not_sign_in(session):
    c = cfg(allowed_email_domains="azienda.it, altra.it")
    user, _ = sso.provision_and_sync(session, claims("mario@azienda.it"), c)
    assert user.id  # allowed domain: signs in

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("estraneo@altrove.example"), c)
    assert e.value.code == "not_allowed"
    assert session.exec(select(User).where(User.email == "estraneo@altrove.example")).first() is None


def test_group_required_to_sign_in(session):
    c = cfg(group_allowlist="analytics", require_allowlisted_group=True)
    make_group(session, "analytics")

    user, _ = sso.provision_and_sync(session, claims("dentro@x.local", groups=["analytics"]), c)
    assert user.id

    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("fuori@x.local", groups=["altro"]), c)
    assert e.value.code == "not_allowed"


def test_the_admin_group_is_enough_to_sign_in(session):
    """Whoever is in the group that grants admin must not be listed twice."""
    c = cfg(group_allowlist="analytics", require_allowlisted_group=True,
            superuser_group="tabularia-admins")
    user, _ = sso.provision_and_sync(session, claims("capo@x.local", groups=["tabularia-admins"]), c)
    assert user.is_superuser is True


def test_contradictory_configuration_says_so(session):
    """Requiring an allowed group without listing any would block everyone silently: better
    an explicit error."""
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("x@x.local"), cfg(require_allowlisted_group=True))
    assert e.value.code == "config_error"


# ── JIT provisioning + synchronization ───────────────────────────────────────
def test_creates_the_user_at_first_login_and_maps_the_groups(session):
    make_group(session, "analytics")
    c = claims("alice@x.local", name="Alice", groups=["/analytics", "sconosciuto"])
    user, detail = sso.provision_and_sync(session, c, cfg())
    assert user.id and user.email == "alice@x.local" and user.full_name == "Alice"
    assert user.hashed_password is None  # an SSO-only user
    assert user.oidc_subject == "sub-of-alice@x.local"  # identity pinned at once
    assert detail["created"] is True and detail["groups_added"] == ["analytics"]
    # 'sconosciuto' does not exist in Tabularia and auto_create is off: ignored
    assert group_names(session, user) == {"analytics"}


def test_second_login_does_not_duplicate_and_completes_the_name(session):
    make_group(session, "analytics")
    c = claims("alice@x.local", groups=["analytics"])
    user, _ = sso.provision_and_sync(session, c, cfg())
    user2, detail = sso.provision_and_sync(session, {**c, "name": "Alice R."}, cfg())
    assert user2.id == user.id and detail["created"] is False and detail["groups_added"] == []
    assert user2.full_name == "Alice R."
    assert len(session.exec(select(UserGroupLink)).all()) == 1


def test_authoritative_removes_the_groups_no_longer_in_the_claim(session):
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


def test_additive_removes_nothing(session):
    analytics, finance = make_group(session, "analytics"), make_group(session, "finance")
    user = make_user(session, email="bob@x.local")
    session.add(UserGroupLink(user_id=user.id, group_id=finance.id))
    session.commit()

    _, detail = sso.provision_and_sync(
        session, claims("bob@x.local", groups=["analytics"], email_verified=True), cfg(authoritative=False)
    )
    assert group_names(session, user) == {"analytics", "finance"}
    assert detail["groups_removed"] == []


def test_allowlist_filters_the_idps_groups(session):
    make_group(session, "analytics")
    make_group(session, "segreti")
    user, _ = sso.provision_and_sync(
        session, claims("c@x.local", groups=["analytics", "segreti"]), cfg(group_allowlist="analytics"),
    )
    assert group_names(session, user) == {"analytics"}


def test_auto_create_creates_the_missing_groups(session):
    user, _ = sso.provision_and_sync(
        session, claims("d@x.local", groups=["nuovo-team"]), cfg(auto_create_groups=True),
    )
    assert group_names(session, user) == {"nuovo-team"}
    assert session.exec(select(Group).where(Group.name == "nuovo-team")).first() is not None


def test_superuser_group_grants_and_revokes(session):
    c = cfg(superuser_group="tabularia-admins")
    user, detail = sso.provision_and_sync(
        session, claims("e@x.local", groups=["tabularia-admins"]), c
    )
    assert user.is_superuser is True and detail["superuser_changed"] is True
    # the admin group does NOT become a project group
    assert group_names(session, user) == set()
    user, detail = sso.provision_and_sync(session, claims("e@x.local", groups=[]), c)
    assert user.is_superuser is False and detail["superuser_changed"] is True


def test_a_token_without_the_groups_claim_is_no_groups_and_the_audit_says_so(session):
    # Keycloak's group mapper writes no claim at all for a person in no group (Entra does the
    # same with roles): an administrator taken out of every group must lose the rights.
    # A claim name set wrong («grups») looks the same: the audit names it.
    make_group(session, "analytics")
    c = cfg(superuser_group="tabularia-admins", authoritative=True)
    user, _ = sso.provision_and_sync(session, claims("g@x.local", groups=["tabularia-admins", "analytics"]), c)
    assert user.is_superuser is True and group_names(session, user) == {"analytics"}
    user, detail = sso.provision_and_sync(session, claims("g@x.local"), c)  # no «groups» key
    assert user.is_superuser is False and group_names(session, user) == set()
    assert detail["groups_claim_missing"] == "groups" and detail["groups_removed"] == ["analytics"]
    user, detail = sso.provision_and_sync(session, claims("g@x.local", groups=["analytics"]),
                                          cfg(superuser_group="tabularia-admins", authoritative=True, groups_claim="grups"))
    assert detail["groups_claim_missing"] == "grups"


def test_disabled_user_does_not_sign_in_through_sso(session):
    make_user(session, email="f@x.local", is_active=False)
    with pytest.raises(sso.SsoError) as e:
        sso.provision_and_sync(session, claims("f@x.local", groups=[], email_verified=True), cfg())
    assert e.value.code == "user_disabled"


def test_without_superuser_group_the_admin_state_is_not_touched(session):
    make_user(session, email="g@x.local", is_superuser=True)
    user, _ = sso.provision_and_sync(
        session, claims("g@x.local", groups=[], email_verified=True), cfg()
    )
    assert user.is_superuser is True  # managed by hand by the admin, SSO does not decide


# ── Routes: behaviour when SSO is off or the flow is tampered with ───────────
class FakeRequest:
    """The bare minimum for the routes: cookie, header and client (for the audit)."""

    def __init__(self, cookies=None, headers=None, scheme="http"):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.url = type("U", (), {"scheme": scheme})()
        self.client = type("C", (), {"host": "127.0.0.1"})()


def test_public_config_exposes_nothing_sensitive(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: Settings(oidc={
        "issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "segretissimo",
        "button_label": "Entra con Keycloak",
    }))
    monkeypatch.setattr(sso, "get_settings", config_mod.get_settings)
    pub = sso.public_config()
    assert pub == {"enabled": True, "button_label": "Entra con Keycloak"}
    assert "segretissimo" not in str(pub) and "issuer" not in pub


def test_login_and_callback_with_sso_off_do_not_blow_up(session):
    from app.routes import sso as sso_routes

    res = sso_routes.sso_login(FakeRequest())
    assert res.status_code == 303 and "error=sso_disabled" in res.headers["location"]
    res = sso_routes.sso_callback(FakeRequest(), code="x", state="y", session=session)
    assert res.status_code == 303 and "error=sso_disabled" in res.headers["location"]


def test_callback_without_transaction_cookie_is_refused(session, monkeypatch):
    from app.core import config as config_mod
    from app.routes import sso as sso_routes

    enabled = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c",
                             "client_secret": "s", "redirect_uri": "https://gw/auth/sso/callback"})
    monkeypatch.setattr(sso_routes, "get_settings", lambda: enabled)
    monkeypatch.setattr(config_mod, "get_settings", lambda: enabled)
    res = sso_routes.sso_callback(FakeRequest(), code="abc", state="s", session=session)
    assert res.status_code == 303 and "error=tx_missing" in res.headers["location"]
    # the IdP refusing the login must never pass its message on to the user
    res = sso_routes.sso_callback(FakeRequest(), error="access_denied",
                                  error_description="consent required", session=session)
    assert "error=idp_denied" in res.headers["location"]
    assert "consent" not in res.headers["location"]


def test_the_local_login_refuses_an_sso_only_user(session):
    """A user created by SSO (without a local password): no back door with an empty
    password. The local login stays for the users that have one."""
    from app.routes.auth import login
    from app.schemas.models import LoginRequest
    from fastapi import HTTPException, Response

    user, _ = sso.provision_and_sync(session, claims("solo-sso@x.local", groups=[]), cfg())
    assert user.hashed_password is None
    for password in ("", "x", "qualsiasi"):
        with pytest.raises(HTTPException) as e:
            login(LoginRequest(email="solo-sso@x.local", password=password), FakeRequest(), Response(), session)
        assert e.value.status_code == 401


def test_callback_ok_opens_the_internal_session_and_writes_the_audit(session, monkeypatch):
    """The route's happy path: an INTERNAL session in the HttpOnly cookie and no token in the
    URL, the transaction cookie deleted, an audit event with the groups' delta."""
    from app.core import config as config_mod
    from app.core.security import decode_access_token
    from app.models import AuditLog, UserSession
    from app.routes import sso as sso_routes

    enabled = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
                             "redirect_uri": "https://gw/auth/sso/callback",
                             "post_login_url": "https://app.example/auth/callback"})
    monkeypatch.setattr(sso_routes, "get_settings", lambda: enabled)
    monkeypatch.setattr(config_mod, "get_settings", lambda: enabled)

    user = make_user(session, email="h@x.local")
    monkeypatch.setattr(sso_routes.sso, "login_user",
                        lambda s, code, tx, cfg, request=None: (user, {"created": False, "groups_added": ["analytics"]}))

    tx_cookie = sso.issue_tx_token("s1", "n1", "v1")
    res = sso_routes.sso_callback(FakeRequest({sso.TX_COOKIE: tx_cookie}), code="ok", state="s1", session=session)

    assert res.status_code == 303
    # the token does not go through the URL: neither in the fragment nor elsewhere
    assert res.headers["location"] == "https://app.example/auth/callback"
    cookies = res.headers.getlist("set-cookie")
    user_session = next(c for c in cookies if c.startswith("tab_token="))
    assert "HttpOnly" in user_session and "Path=/" in user_session and "SameSite=lax" in user_session
    # it is Tabularia's INTERNAL TOKEN, with a session open like the local login's
    payload = decode_access_token(user_session.split(";")[0].split("=", 1)[1])
    assert int(payload["sub"]) == user.id
    row = session.get(UserSession, payload["sid"])
    assert row is not None and row.user_id == user.id and row.method == "sso"
    # the transaction cookie is single-use
    transaction = next(c for c in cookies if c.startswith("tab_sso_tx="))
    assert 'tab_sso_tx=""' in transaction or "Max-Age=0" in transaction

    entry = session.exec(select(AuditLog).where(AuditLog.action == "auth.sso_login")).first()
    assert entry is not None and entry.actor_id == user.id and "analytics" in (entry.detail or "")


# ── admin and observer groups are not handed out by name (AUDIT-2026-09-19 M2) ─
def _special_group(session, name, **flags):
    g = Group(name=name, **flags)
    session.add(g)
    session.commit()
    session.refresh(g)
    return g


def test_an_idp_group_named_like_an_admin_group_does_not_make_an_administrator(session):
    """Whoever could get a group called like Tabularia's admin group onto their token
    (self-service groups on Entra, a loose Keycloak realm) became administrator. The
    IdP grants administration only through OIDC__SUPERUSER_GROUP."""
    from app.services.permissions import is_admin, is_observer

    _special_group(session, "admins", is_admin=True)
    _special_group(session, "guests", is_observer=True)
    make_group(session, "analytics")
    user, detail = sso.provision_and_sync(session, claims("mario@x.local", groups=["admins", "guests", "analytics"]), cfg())
    assert group_names(session, user) == {"analytics"}
    assert not is_admin(session, user) and not is_observer(session, user)
    assert detail["groups_added"] == ["analytics"]


def test_an_observer_group_can_be_mapped_when_allowlisted(session):
    _special_group(session, "guests", is_observer=True)
    user, _ = sso.provision_and_sync(session, claims("ospite@x.local", groups=["guests"]), cfg(group_allowlist="guests"))
    assert group_names(session, user) == {"guests"}


def test_an_admin_group_given_in_tabularia_survives_an_authoritative_sso_login(session):
    """Those memberships are managed in Tabularia: SSO neither adds nor removes them."""
    admins = _special_group(session, "admins", is_admin=True)
    user, _ = sso.provision_and_sync(session, claims("capo@x.local"), cfg())
    session.add(UserGroupLink(user_id=user.id, group_id=admins.id))
    session.commit()
    user, detail = sso.provision_and_sync(session, claims("capo@x.local", groups=[]), cfg(authoritative=True))
    assert "admins" in group_names(session, user) and detail["groups_removed"] == []


# ── Robustness of the sign-in (problems seen in a company with Keycloak) ─────────────
def _sso_on(monkeypatch, redirect="https://tabularia.acme.example/api/auth/sso/callback"):
    from app.core import config as config_mod
    from app.routes import sso as sso_routes

    enabled = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
                             "redirect_uri": redirect, "post_login_url": "https://tabularia.acme.example/auth/callback"})
    for mod in (sso_routes, config_mod, sso):
        monkeypatch.setattr(mod, "get_settings", lambda: enabled)
    monkeypatch.setattr(sso, "discovery", lambda c: {"authorization_endpoint": "https://idp/auth", "issuer": "https://idp/realms/t"})
    return sso_routes


def _tx_cookie(res) -> tuple[str, str]:
    header = res.headers["set-cookie"]
    name, rest = header.split("=", 1)
    return name, header


def test_a_second_sign_in_does_not_overwrite_the_first(monkeypatch):
    """A double click, another tab, the Back button: each sign-in keeps its own cookie,
    and the first one, finished, finds its own (it came back as state_mismatch)."""
    routes = _sso_on(monkeypatch)
    first = routes.sso_login(FakeRequest(headers={"host": "tabularia.acme.example", "x-forwarded-proto": "https"}))
    second = routes.sso_login(FakeRequest(headers={"host": "tabularia.acme.example", "x-forwarded-proto": "https"}))
    (n1, _), (n2, _) = _tx_cookie(first), _tx_cookie(second)
    assert n1 != n2 and n1.startswith(sso.TX_COOKIE + "_")
    state1 = first.headers["location"].split("state=")[1].split("&")[0]
    assert sso.tx_cookie_name(state1) == n1


def test_the_transaction_cookie_is_secure_as_the_browser_is(monkeypatch):
    """Secure follows the browser's scheme, as the session's cookie: decided from an https
    redirect URI, a sign-in started on http lost its cookie (the browser refuses it)."""
    routes = _sso_on(monkeypatch)
    on_http = _tx_cookie(routes.sso_login(FakeRequest(headers={"host": "tabularia.acme.example"})))[1]
    on_https = _tx_cookie(routes.sso_login(FakeRequest(headers={"host": "tabularia.acme.example", "x-forwarded-proto": "https"})))[1]
    assert "Secure" not in on_http and "Secure" in on_https


def test_a_missing_cookie_says_which_addresses_differ(monkeypatch, session, caplog):
    routes = _sso_on(monkeypatch)
    with caplog.at_level("WARNING"):
        routes.sso_login(FakeRequest(headers={"host": "10.0.0.7"}))
        res = routes.sso_callback(FakeRequest(headers={"host": "tabularia.acme.example", "x-forwarded-proto": "https"}),
                                  code="abc", state="s", session=session)
    assert "error=tx_missing" in res.headers["location"]
    log = caplog.text
    assert "sign-in started at http://10.0.0.7" in log and "https://tabularia.acme.example" in log


def test_clocks_a_little_apart_do_not_break_the_sign_in():
    """A token signed by a machine whose clock is ahead — another gateway replica, the
    identity provider — is "not yet valid" by a strict reading: a margin of a minute."""
    import jwt as pyjwt

    from app.core.config import get_settings
    from app.core.security import decode_access_token

    secret = get_settings().jwt.secret.get_secret_value()
    ahead = int(time.time()) + 5
    tx = pyjwt.encode({"typ": sso.TX_TOKEN_TYPE, "state": "s", "iat": ahead, "exp": ahead + 600}, secret, algorithm="HS256")
    assert sso.read_tx_token(tx, "s")["state"] == "s"
    session_token = pyjwt.encode({"sub": "1", "sid": "x", "iat": ahead, "exp": ahead + 600}, secret, algorithm="HS256")
    assert decode_access_token(session_token)["sub"] == "1"


def test_an_id_token_from_a_provider_whose_clock_is_ahead_is_accepted(monkeypatch):
    """Keycloak on another machine, its clock two seconds ahead: its id_token's iat is in
    the gateway's future. Refused, every sign-in failed with token_invalid."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ahead = int(time.time()) + 2
    claims = {"iss": "https://idp/realms/t", "aud": "c", "sub": "u1", "nonce": "n", "iat": ahead, "exp": ahead + 300}
    token = pyjwt.encode(claims, key, algorithm="RS256")
    signing = type("K", (), {"key": key.public_key()})()
    monkeypatch.setattr(sso, "discovery", lambda c: {"issuer": "https://idp/realms/t", "jwks_uri": "https://idp/jwks",
                                                     "id_token_signing_alg_values_supported": ["RS256"]})
    monkeypatch.setattr(sso, "_jwks_client", lambda uri, t: type("J", (), {"get_signing_key_from_jwt": lambda self, tok: signing})())
    assert sso.validate_id_token(cfg(client_id="c"), token, "n")["sub"] == "u1"


# ── Groups from a path of the token (Keycloak's realm and client roles) ──────────────
def test_a_path_reaches_nested_claims_and_keys_with_dots():
    claims = {"groups": ["/analytics"], "realm_access": {"roles": ["finance"]},
              "resource_access": {"tabularia.gateway": {"roles": ["analyst"]}, "account": {"roles": ["view"]}}}
    assert sso.claim_at(claims, "realm_access.roles") == ["finance"]
    assert sso.claim_at(claims, "resource_access.tabularia.gateway.roles") == ["analyst"]
    assert sso.claim_at(claims, "realm_access.nothing") is None and sso.claim_at(claims, "nope") is None
    both = cfg(groups_claim="groups, realm_access.roles,resource_access.tabularia.gateway.roles")
    assert sso.normalize_groups(claims, both) == {"analytics", "finance", "analyst"}


def _keycloak(monkeypatch, *, access_extra=None, userinfo=None, sub="u1"):
    """A provider like Keycloak by default: groups in the id_token, roles only in the
    access token."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    base = {"iss": "https://idp/realms/t", "sub": "u1", "iat": now, "exp": now + 300}
    id_token = pyjwt.encode({**base, "aud": "c", "nonce": "n", "email": "mario@acme.example", "groups": ["analytics"]},
                            key, algorithm="RS256")
    access = pyjwt.encode({**base, "sub": sub, "realm_access": {"roles": ["finance"]}, **(access_extra or {})},
                          key, algorithm="RS256")
    signing = type("K", (), {"key": key.public_key()})()
    monkeypatch.setattr(sso, "discovery", lambda c=None, **k: {
        "issuer": "https://idp/realms/t", "jwks_uri": "https://idp/jwks", "userinfo_endpoint": "https://idp/userinfo",
        "id_token_signing_alg_values_supported": ["RS256"], "token_endpoint": "https://idp/token",
        "authorization_endpoint": "https://idp/auth"})
    monkeypatch.setattr(sso, "_jwks_client", lambda uri, t: type("J", (), {"get_signing_key_from_jwt": lambda self, tok: signing})())
    monkeypatch.setattr(sso, "exchange_code", lambda c, code, v: {"id_token": id_token, "access_token": access})
    asked = []

    def fake_get(url, headers=None, timeout=None, verify=True):
        asked.append(url)
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: userinfo or {"sub": "u1"}})()
    monkeypatch.setattr(sso.httpx, "get", fake_get)
    return asked


def test_realm_roles_come_from_the_access_token(session, monkeypatch):
    asked = _keycloak(monkeypatch)
    for name in ("analytics", "finance"):
        make_group(session, name)
    user, detail = sso.login_user(session, "code", {"cv": "v", "nonce": "n"}, cfg(client_id="c", groups_claim="groups,realm_access.roles"))
    assert sorted(detail["groups"]) == ["analytics", "finance"]
    assert asked == []  # found in the access token: no call to userinfo


def test_an_access_token_of_someone_else_is_not_read(session, monkeypatch):
    _keycloak(monkeypatch, sub="someone-else")
    make_group(session, "finance")
    _, detail = sso.login_user(session, "code", {"cv": "v", "nonce": "n"}, cfg(client_id="c", groups_claim="realm_access.roles"))
    assert detail["groups"] == []


def test_userinfo_when_neither_token_has_the_path(session, monkeypatch):
    asked = _keycloak(monkeypatch, userinfo={"sub": "u1", "org": {"teams": ["sales"]}})
    make_group(session, "sales")
    _, detail = sso.login_user(session, "code", {"cv": "v", "nonce": "n"}, cfg(client_id="c", groups_claim="org.teams"))
    assert detail["groups"] == ["sales"] and asked == ["https://idp/userinfo"]


# ── A provider whose certificate a company's own authority signed ─────────────────────
def _company_ca(tmp_path):
    """A certificate authority of a company, as a PEM file."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ACME Internal Root CA")])
    now = dt.datetime.now(dt.timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    path = tmp_path / "ca.crt"
    path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_the_company_authority_is_trusted_besides_the_public_ones(tmp_path):
    sso.reset_discovery_cache()
    assert sso.tls(cfg()) is True  # without one: the public authorities, as httpx does
    ctx = sso.tls(cfg(ca_file=_company_ca(tmp_path)))
    subjects = {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}
    assert "ACME Internal Root CA" in subjects and len(subjects) > 50  # the company's and the public ones
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_an_untrusted_certificate_says_so_and_what_to_do(monkeypatch):
    import httpx as real_httpx

    sso.reset_discovery_cache()

    def refused(url, timeout=None, verify=True):
        raise real_httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
    monkeypatch.setattr(sso.httpx, "get", refused)
    with pytest.raises(sso.SsoError) as e:
        sso.discovery(cfg(issuer="https://sso.acme.internal/realms/acme"))
    assert e.value.code == "idp_untrusted" and "sso.acme.internal" in str(e.value) and "OIDC__CA_FILE" in str(e.value)

    def down(url, timeout=None, verify=True):
        raise real_httpx.ConnectError("[Errno 111] Connection refused")
    monkeypatch.setattr(sso.httpx, "get", down)
    with pytest.raises(sso.SsoError) as e:
        sso.discovery(cfg(issuer="https://sso.acme.internal/realms/acme"), force=True)
    assert e.value.code == "idp_unreachable"


def test_a_ca_file_that_is_not_one_stops_the_start(tmp_path):
    bad = tmp_path / "ca.crt"
    bad.write_text("not a certificate")
    s = Settings(oidc={"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
                       "redirect_uri": "https://t/api/auth/sso/callback", "post_login_url": "https://t/auth/callback",
                       "ca_file": str(bad)})
    with pytest.raises(RuntimeError, match="OIDC__CA_FILE"):
        s.check_sso_config()


def test_the_installer_may_accept_an_untrusted_certificate(tmp_path, caplog):
    """OIDC__VERIFY_TLS=false: the provider's certificate is accepted, whatever signed it,
    for every call (the keys too), and the start says so."""
    sso.reset_discovery_cache()
    off = cfg(verify_tls=False, ca_file=_company_ca(tmp_path))
    assert sso.tls(off) is False  # it wins over a CA file
    s = Settings(oidc={"issuer": "https://sso.acme.internal/realms/acme", "client_id": "c", "client_secret": "s",
                       "redirect_uri": "https://t/api/auth/sso/callback", "post_login_url": "https://t/auth/callback",
                       "verify_tls": False})
    with caplog.at_level("WARNING"):
        s.check_sso_config()
    assert "NOT checked" in caplog.text and "sso.acme.internal" in caplog.text


def _bad_https_server(tmp_path):
    """A real https server whose certificate is everything a check refuses: self-signed,
    expired, and made out to another name. It serves a discovery document and the keys."""
    import datetime as dt
    import http.server
    import json
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wrong.example")])
    then = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(then).not_valid_after(then + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("wrong.example")]), critical=False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "srv.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "srv.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(signer.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    state = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            base = state["base"]
            body = ({"issuer": f"{base}/realms/t", "authorization_endpoint": f"{base}/auth",
                     "token_endpoint": f"{base}/token", "jwks_uri": f"{base}/jwks"}
                    if self.path.endswith("openid-configuration") else {"keys": [jwk]})
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "srv.crt", tmp_path / "srv.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    state["base"] = f"https://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state["base"], signer


def test_an_unchecked_certificate_is_accepted_for_real_and_a_checked_one_refused(tmp_path, monkeypatch):
    """Against a real server: a self-signed, expired certificate for another name. Checked,
    every call is refused as idp_untrusted; with OIDC__VERIFY_TLS=false, the discovery
    document and the signing keys are read."""
    import jwt as pyjwt

    from app.core.config import get_settings

    server, base, signer = _bad_https_server(tmp_path)
    try:
        sso.reset_discovery_cache()
        with pytest.raises(sso.SsoError) as e:
            sso.discovery(cfg(issuer=f"{base}/realms/t"))
        assert e.value.code == "idp_untrusted"

        off = cfg(issuer=f"{base}/realms/t", verify_tls=False)
        sso.reset_discovery_cache()
        doc = sso.discovery(off)
        assert doc["jwks_uri"] == f"{base}/jwks"
        # the keys, read by PyJWKClient on the same unchecked terms
        monkeypatch.setattr(get_settings().oidc, "verify_tls", False)
        token = pyjwt.encode({"sub": "x"}, signer, algorithm="RS256", headers={"kid": "k1"})
        key = sso._jwks_client(doc["jwks_uri"], 5).get_signing_key_from_jwt(token)
        assert key.key_id == "k1"
    finally:
        server.shutdown()
        sso.reset_discovery_cache()
