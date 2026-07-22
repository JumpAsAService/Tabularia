# Example — Keycloak → Tabularia group mapping

A concrete, runnable example of the design in
[`../../design/sso-group-mapping.md`](../../design/sso-group-mapping.md): a Keycloak
realm whose **groups** land in the OIDC token as a `groups` claim, ready to be mapped
1:1 onto Tabularia groups by name.

> The gateway OIDC front door is a **proposal** (see the design note). This example
> gives you a working IdP + the exact token shape and gateway config to target, so the
> integration is a matter of wiring, not guesswork. You can already inspect the token
> today with `curl` (below) to confirm the `groups` claim.

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

## 2. Create a test user and assign groups

In the admin console (realm **tabularia**):

1. **Users → Add user**: username `alice`, email `alice@example.com`, email verified on.
2. **Credentials**: set a password (temporary off).
3. **Groups → Join**: add `analytics` (and `tabularia-admins` to test superuser).

Mirror the group **names** in Tabularia so the mapping resolves (or set
`OIDC__AUTO_CREATE_GROUPS=true` to have them created on first login).

## 3. Inspect the token (works today, no gateway changes needed)

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

That `groups` array is exactly what `provision_and_sync()` in the design note maps
onto `user_groups`.

## 4. Point the gateway at Keycloak

Copy the OIDC block into `infrastructure/.env`:

```bash
cat docs/examples/keycloak/gateway.env.example >> infrastructure/.env
```

Adjust `OIDC__ISSUER` / `OIDC__REDIRECT_URI` for your host, and **rotate
`OIDC__CLIENT_SECRET`** (the example secret is dev-only). The gateway self-configures
from `${OIDC__ISSUER}/.well-known/openid-configuration`.

## Entra ID (MSAL) note

The same design works with Microsoft Entra ID. Differences:

- Prefer **app roles** over groups: set `OIDC__GROUPS_CLAIM=roles`. (Entra can also
  emit a `groups` claim of **group object IDs**, not names — roles map to readable
  names and avoid the large-groups overage claim.)
- Issuer is `https://login.microsoftonline.com/<tenant-id>/v2.0`; the rest of the flow
  (JWKS validation, JIT provision, name-keyed sync) is identical.
