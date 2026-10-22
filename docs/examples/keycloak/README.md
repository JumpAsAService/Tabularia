<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# Example — Keycloak → Tabularia group mapping

A runnable Keycloak realm whose **groups** land in the OIDC token as a `groups` claim,
mapped 1:1 onto Tabularia groups by name. The gateway side is implemented: see
[`../../design/sso-group-mapping.md`](../../design/sso-group-mapping.md) for the design
and [`gateway/app/services/sso.py`](../../../gateway/app/services/sso.py) for the code.

SSO is **optional**: with `OIDC__ISSUER` unset, Tabularia keeps local email + password
login and the login page shows no SSO button.

This folder is for trying SSO on a laptop with Docker Compose. To connect your company's
Keycloak to Tabularia installed with Helm, follow
[Single sign-on with Keycloak](../../sso-keycloak.md).

## What's here

| file | purpose |
|---|---|
| `docker-compose.keycloak.yml` | a dev Keycloak that auto-imports the realm |
| `tabularia-realm.json` | realm `tabularia`: confidential client `tabularia-gateway` with a group-membership mapper, and sample groups |
| `gateway.env.example` | the `OIDC__*` block to drop into `infrastructure/.env` |

## 1. Boot Keycloak with the realm

```bash
cd docs/examples/keycloak
docker compose -f docker-compose.keycloak.yml up -d
# Admin console: http://localhost:8080  (admin / admin)
```

The realm imports with:

- **Client** `tabularia-gateway` — confidential, standard (authorization-code) flow,
  PKCE (S256), redirect URI `http://localhost:8000/auth/sso/callback`.
- A **Group Membership** mapper attached to the client, adding a `groups` claim to the
  ID/access token **without** the leading path (`Full group path = off`), so the claim
  is `["analytics"]`, not `["/analytics"]`. It's on the client (not a separate client
  scope) so the claim is emitted on every token and no custom scope must be requested.
- **Groups**: `analytics`, `finance`, `tabularia-admins`.

These group names are what you create in Tabularia (Admin → Groups). Membership of
`tabularia-admins` is what `OIDC__SUPERUSER_GROUP` keys on.

## 2. One address for both sides (dev only)

The issuer must be reachable **by the same URL** from your browser and from inside the
gateway container, because it is both the redirect target and the value signed into the
token. `localhost:8080` means "the host" to your browser and "the container itself" to
the gateway, so it cannot work as is.

**Simplest: the bridge IP of the app network.** The host owns that address on its
Docker bridge, and every container on the network reaches the host through it, while
Keycloak publishes port 8080 on all interfaces:

```bash
docker network inspect dataprep-network -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
# e.g. 172.22.0.1  →  OIDC__ISSUER=http://172.22.0.1:8080/realms/tabularia
```

No `/etc/hosts` edit, no compose change. Keycloak serves plain `SameSite=Lax` session
cookies on that private address, which browsers accept over HTTP. The IP stays stable
as long as the network is not recreated.

**Alternative: a hostname.** Map one to the host on both sides:

```bash
echo "127.0.0.1 keycloak.local" | sudo tee -a /etc/hosts
```

```yaml
  gateway:            # infrastructure/docker-compose.yml
    extra_hosts:
      - "keycloak.local:host-gateway"
```

and use `http://keycloak.local:8080/realms/tabularia` as the issuer.

In production this wrinkle disappears: the IdP has one public DNS name.

## 3. Create a test user and assign groups

In the admin console (realm **tabularia**):

1. **Users → Add user**: username `alice`, email `alice@example.com`, email verified on.
2. **Credentials**: set a password (temporary off).
3. **Groups → Join**: add `analytics` (and `tabularia-admins` to test superuser).

Mirror the group **names** in Tabularia so the mapping resolves (or set
`OIDC__AUTO_CREATE_GROUPS=true` to have them created on first login).

## 4. Inspect the token (optional sanity check)

Confirm the `groups` claim shape with the direct-grant flow:

```bash
curl -s http://localhost:8080/realms/tabularia/protocol/openid-connect/token \
  -d grant_type=password \
  -d client_id=tabularia-gateway \
  -d client_secret=tabularia-dev-secret \
  -d username=alice -d password=YOUR_PASSWORD \
  -d scope='openid profile email' | \
  cut -d. -f2 | base64 -d 2>/dev/null | python3 -m json.tool
```

You should see:

```json
{
  "email": "alice@example.com",
  "name": "Alice ...",
  "groups": ["analytics", "tabularia-admins"]
}
```

That `groups` array is what `provision_and_sync()` maps onto `user_groups`.

**Roles instead of groups.** If the team keeps the access in Keycloak **roles**, point
`OIDC__GROUPS_CLAIM` at them with the path they have in the token's JSON:

| What | `OIDC__GROUPS_CLAIM` |
|---|---|
| realm roles | `realm_access.roles` |
| roles of a client | `resource_access.<client id>.roles`, e.g. `resource_access.tabularia-gateway.roles` |
| groups and realm roles together | `groups,realm_access.roles` |

Each name maps onto the Tabularia group with the same name, as groups do. Keycloak puts
`realm_access` and `resource_access` in the **access token** only, by default: the gateway
reads them there (it checks the signature with the realm's keys, the issuer, the expiry
and that it is the same person) when the id_token does not carry the path, then asks
userinfo. Keycloak adds its own realm roles too (`offline_access`, `uma_authorization`,
`default-roles-<realm>`): with `OIDC__AUTO_CREATE_GROUPS=false` they match no group and
are ignored; `OIDC__GROUP_ALLOWLIST` keeps out whatever else should not count.

## 5. Point the gateway at Keycloak

Copy the OIDC block into `infrastructure/.env`:

```bash
cat docs/examples/keycloak/gateway.env.example >> infrastructure/.env
docker compose -f infrastructure/docker-compose.yml up -d gateway
```

Adjust `OIDC__ISSUER` / `OIDC__REDIRECT_URI` for your host, and **rotate
`OIDC__CLIENT_SECRET`** (the example secret is dev-only). The gateway self-configures
from `${OIDC__ISSUER}/.well-known/openid-configuration` and refuses to start if the
block is half-filled.

Check it took:

```bash
curl -s http://localhost:8000/auth/sso/config     # {"enabled":true,...}
```

Then open http://localhost:3000/login — the SSO button is there. First sign-in creates
the Tabularia user and syncs the groups; the audit log records `auth.sso_login` with the
group delta.

## Entra ID (MSAL)

Same flow, three differences:

- **Issuer** is `https://login.microsoftonline.com/<tenant-id>/v2.0`.
- **Prefer app roles over groups**: set `OIDC__GROUPS_CLAIM=roles`. Entra's `groups`
  claim carries group *object IDs*, not names, and overflows to a Graph lookup for users
  in many groups; app roles are readable names defined on the app registration.
- Register the redirect URI under **Web** (not SPA) since the gateway is a confidential
  client, and create a client secret for it. The `email` claim may be absent: the
  gateway falls back to `preferred_username` / `upn`.

Everything else (JWKS validation, JIT provisioning, name-keyed group sync, the policy
toggles) is identical.

## Troubleshooting

A failed sign-in lands back on the login page with `?sso_error=<code>`, and for the
common ones a sentence telling the person what to do; the full reason is in the gateway
log, and the code in the audit log (*Failed SSO login*), never more in the browser, to
avoid leaking IdP or config details.

| code | meaning |
|---|---|
| `sso_disabled` | `OIDC__ISSUER` not set on the gateway |
| `idp_untrusted` | the provider's certificate is signed by an authority the gateway does not trust (a company one): give its certificate in `OIDC__CA_FILE` (the chart's `oidc.caCert` or `oidc.caConfigMap`), or accept it with `OIDC__VERIFY_TLS=false` — see [Single sign-on with Keycloak](../../sso-keycloak.md#a-certificate-the-gateway-does-not-trust) for what that costs |
| `idp_unreachable` | discovery or token endpoint not reachable from the gateway container (usually the hostname wrinkle in step 2) |
| `idp_denied` | the IdP refused the login (consent, disabled account, policy) |
| `tx_missing` | the sign-in's cookie did not come back with the callback. Almost always the sign-in **started at another address** than `OIDC__REDIRECT_URI`'s: another host name for the same server, an IP, `http` against `https`. The browser keeps the cookies of each address apart. The gateway logs `sign-in started at <address> but OIDC__REDIRECT_URI is on <address>` when it begins: have people open Tabularia at the redirect URI's address (or register theirs too). Otherwise the browser blocks cookies |
| `tx_expired` | the sign-in took longer than `OIDC__LOGIN_TX_TTL_SECONDS` (10 minutes; a first sign-in that asks to set a password or a second factor can), or the clocks of the gateway's machines are more than a minute apart: check NTP on the nodes |
| `state_mismatch` | the `state` Keycloak sent back is not the one of this sign-in (a tampered link, or a callback of an older sign-in replayed): start again. A second sign-in started meanwhile — a double click, another tab — no longer causes it: each sign-in has its own cookie |
| `code_rejected` | the IdP refused the code exchange: usually a wrong client secret or a redirect URI that does not match exactly |
| `token_invalid`, `nonce_mismatch`, `jwks_error` | id_token failed validation (signature, `iss`, `aud`, `exp`, `nonce`). The gateway allows a minute between its clock and Keycloak's; further apart, an id_token is "not yet valid" or already expired: check NTP on both. A wrong `iss` is usually Keycloak's hostname (`KC_HOSTNAME`) differing from `OIDC__ISSUER` |
| `no_email` | the token carries no email, UPN or `preferred_username` in email form |
| `no_identity` | the token carries no `iss`/`sub`, so it identifies nobody. Tabularia recognises a user by that stable pair, never by the email alone |
| `email_unverified` | an account with this email already exists and the IdP does not assert the address as **verified**, so it will not be claimed. Linking an existing account happens once and requires proof; a brand-new account does not |
| `identity_conflict` | that account is already linked to a different identity of the IdP. Two people cannot claim the same user |
| `user_disabled` | the user exists in Tabularia but is deactivated |
<!-- {% endraw %} -->
