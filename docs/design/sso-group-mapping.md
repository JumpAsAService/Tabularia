# Design note — SSO & external group mapping (OIDC)

**Status:** proposal (not yet implemented). This note describes how an external
identity provider (Keycloak, Microsoft Entra ID / MSAL, Auth0, Okta, …) would plug
into Tabularia and how its groups/roles map onto Tabularia groups, so consumers can
evaluate the effort and so a future implementation stays aligned.

A runnable Keycloak example lives in [`../examples/keycloak/`](../examples/keycloak/).

---

## Goal

Let a self-hosting consumer authenticate users against their own IdP and have the
IdP's groups (or app roles) drive Tabularia's RBAC — **without touching** the
authorization layer, the audit trail, or saved flows.

## Current auth model (recap)

- **Local only today.** `POST /auth/login` verifies an email + bcrypt password and
  issues an internal **JWT** (`HS256`, `sub = user.id`), see
  [`gateway/app/routes/auth.py`](../../gateway/app/routes/auth.py) and
  [`gateway/app/core/security.py`](../../gateway/app/core/security.py).
- **Groups are name-keyed.** `groups.name` is `unique`; membership is the
  `user_groups` M2M link — [`gateway/app/models/user.py`](../../gateway/app/models/user.py).
- **RBAC reads only local tables.** `user_group_ids()` → `readable_project_ids()` in
  [`gateway/app/services/permissions.py`](../../gateway/app/services/permissions.py)
  resolves project permissions from group membership. It is fully decoupled from *how*
  identity was established.
- **Auth has exactly two seams.** The login route (token issuance) and
  `get_current_user` (token acceptance) in
  [`gateway/app/deps/auth.py`](../../gateway/app/deps/auth.py).

Because RBAC only reads `user_groups`, the mapping problem reduces to **keeping
`user_groups` in sync with the IdP claim at login**. Everything downstream is unchanged.

## Design principles

1. **Keep the internal JWT.** After validating the IdP token, issue the *existing*
   Tabularia JWT via `create_access_token(user.id)`. `get_current_user`, RBAC, and
   audit stay byte-for-byte the same. This is the smallest-blast-radius choice.
2. **Groups map by name.** An IdP claim `groups: ["analytics", "finance"]` maps 1:1
   onto `Group.name`. No IDs, no external coupling in the RBAC layer.
3. **JIT provisioning.** Create the `User` on first SSO login from the token's `email`
   / `name` claims; never require a pre-created local account.
4. **Local auth stays available.** SSO is additive. Break-glass local admin login keeps
   working (important if the IdP is down).

## Flow (Authorization Code + internal JWT)

```
Browser                Gateway                         IdP (Keycloak/Entra)
   │  "Sign in with SSO"  │                                  │
   ├─────────────────────>│  GET /auth/sso/login             │
   │                      ├── 302 authorize (state, nonce) ─>│
   │<──────────────── redirect to IdP login ─────────────────┤
   │  authenticate + consent                                  │
   ├────────────────────── code (redirect_uri) ─────────────>│
   │                      │  GET /auth/sso/callback?code&state│
   │                      ├── token exchange (code) ────────>│
   │                      │<── id_token + access_token ───────┤
   │                      │  validate (JWKS, iss, aud, nonce) │
   │                      │  JIT-provision user               │
   │                      │  sync user_groups from claim      │
   │                      │  issue INTERNAL JWT               │
   │<─── internal JWT (same as local login) ──────────────────┤
   │  ... every later request unchanged (Bearer internal JWT) │
```

## The two seams

### 1. Token acceptance — new SSO routes
Add `GET /auth/sso/login` (redirect to the IdP `authorize` endpoint with `state` +
`nonce`) and `GET /auth/sso/callback` (exchange code, validate token against the IdP
**JWKS**, checking `iss`/`aud`/`exp`/`nonce`). Discovery via the IdP's
`.well-known/openid-configuration`. Local `POST /auth/login` is untouched.

### 2. Group sync — one reconcile function
On successful validation, reconcile membership from the token's group/role claim:

```python
# gateway/app/services/sso.py  (sketch)
def provision_and_sync(session, claims, cfg) -> User:
    email = claims["email"]
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email, full_name=claims.get("name", ""),
                    hashed_password="", is_active=True)   # SSO-only: no local pw
        session.add(user); session.flush()

    idp_groups = set(claims.get(cfg.groups_claim, []))    # e.g. {"analytics","finance"}
    if cfg.group_allowlist:                               # optional: restrict/rename
        idp_groups &= cfg.group_allowlist
    if cfg.superuser_group:
        user.is_superuser = cfg.superuser_group in claims.get(cfg.groups_claim, [])

    # ensure Group rows exist (auto-create is a policy toggle)
    wanted_ids = _resolve_group_ids(session, idp_groups, auto_create=cfg.auto_create)

    current = set(user_group_ids(session, user))
    for gid in wanted_ids - current:
        session.add(UserGroupLink(user_id=user.id, group_id=gid))
    if cfg.authoritative:                                 # IdP owns membership
        for gid in current - wanted_ids:
            session.delete(session.get(UserGroupLink, (user.id, gid)))
    session.commit()
    return user
```

RBAC then works unchanged because `user_group_ids()` reads the very rows this function
maintains.

## Mapping semantics (policy toggles, config-driven)

| Concern | Options | Default |
|---|---|---|
| **Authoritative vs additive** | IdP removes memberships not in the claim / SSO only adds | authoritative |
| **Auto-create groups** | create missing `Group` rows / only map to pre-existing (allowlist) | allowlist |
| **Superuser** | membership of a designated IdP group/role grants `is_superuser` | off |
| **Claim source** | `groups` (Keycloak group membership) or `roles` / app roles (Entra) | `groups` |
| **Name normalization** | strip leading `/` (Keycloak paths), lowercase, optional prefix filter | strip `/` |

## Data-model changes

- `users.hashed_password` is currently `NOT NULL`. Make it **nullable** (or accept an
  empty sentinel) so SSO-only users need no local password. Small forward migration in
  [`gateway/app/db/session.py`](../../gateway/app/db/session.py) (the project does
  lightweight in-code migrations there).
- *(Optional)* add `users.idp` and `users.external_id` to disambiguate identities
  across providers and pin an account to an IdP subject (`sub`). Not required if email
  is the stable key.

No changes to `groups`, `user_groups`, or any RBAC/permission code.

## Configuration (proposed `OIDC__*` block)

Follows the existing nested-settings convention (`env_nested_delimiter="__"`, see
[`gateway/app/core/config.py`](../../gateway/app/core/config.py)):

```dotenv
OIDC__ENABLED=true
OIDC__ISSUER=https://keycloak.example.com/realms/tabularia
OIDC__CLIENT_ID=tabularia-gateway
OIDC__CLIENT_SECRET=...                     # confidential client
OIDC__REDIRECT_URI=https://app.example.com/auth/sso/callback
OIDC__SCOPES=openid profile email groups
OIDC__GROUPS_CLAIM=groups                   # 'roles' for Entra app roles
OIDC__GROUP_ALLOWLIST=                      # empty = allow all
OIDC__AUTO_CREATE_GROUPS=false
OIDC__AUTHORITATIVE=true
OIDC__SUPERUSER_GROUP=tabularia-admins
```

The gateway self-configures from `${OIDC__ISSUER}/.well-known/openid-configuration`
(authorize/token/JWKS endpoints), so only the issuer + client credentials are needed.

## Security considerations

- **Validate the token, don't trust it.** Verify signature against the IdP JWKS
  (cache + rotate), and check `iss`, `aud` (= client_id), `exp`, and the `nonce` bound
  to the login request. Reject unsigned/`alg:none`.
- **CSRF on the callback.** Bind and verify `state`; use PKCE for the code flow.
- **Session lifetime.** The internal JWT TTL (`JWT__ACCESS_TTL_MINUTES`, 12h) governs
  the Tabularia session; group changes at the IdP take effect on the next SSO login.
  For faster deprovisioning, shorten the TTL or re-sync on a schedule.
- **Deprovisioning.** With `AUTHORITATIVE=true`, dropping a user from an IdP group
  removes the corresponding Tabularia access at next login; disabling the user at the
  IdP stops new logins but does not revoke a live JWT until it expires.
- **Audit.** Emit `LOGIN` (and a new `SSO_LOGIN` / `GROUP_SYNC`) audit events through
  the existing `record_audit`, including the resolved group delta.

## Effort estimate

Well-scoped, comparable to one of the existing feature "phases" — **not** a
rearchitecture:

- one new service (`sso.py`: discovery, token validation, provision+sync),
- two routes (`/auth/sso/login`, `/auth/sso/callback`),
- an `OidcSettings` block,
- a nullable-`hashed_password` migration,
- a frontend "Sign in with SSO" button + redirect handling,
- one dependency (`authlib`, which bundles JWKS validation and the code flow).

The **group mapping itself is trivial** (reconciling name-keyed rows); the work is the
OIDC front door.

## Open questions

- Multiple IdPs simultaneously, or one per deployment? (One is simpler; `users.idp`
  leaves the door open.)
- Should group **auto-create** ever be allowed, or always map to admin-curated groups?
  (Allowlist is safer for RBAC hygiene.)
- Do consumers need SCIM-style background sync, or is login-time JIT enough for v1?
  (JIT is enough for the stated use case.)
