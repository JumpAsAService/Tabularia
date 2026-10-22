# Single sign-on with Keycloak

How to let people sign in to Tabularia with your company's Keycloak, and have their
Keycloak groups or roles decide what they see. For whoever installs Tabularia with the
Helm chart and administers the Keycloak realm. Every step below was run against
Keycloak 26 and the chart on a test cluster.

Signing in with an email and a password keeps working next to it, for the administrator
the chart creates: it is the way in when Keycloak is down.

## How it works

1. On the sign-in page a button (*Sign in with …*) sends the browser to Keycloak.
2. Keycloak signs the person in, or recognises a session it already has, and sends the
   browser back to Tabularia's callback address with a one-time code.
3. Tabularia's gateway exchanges the code for the tokens, server to server, with the
   client secret; checks them; creates the account at the first sign-in; and sets the
   person's Tabularia groups from their Keycloak groups or roles.

Two things follow from it:

- **One address.** The sign-in has to start and end at the same address of Tabularia,
  the one registered in Keycloak: a short-lived cookie set when it starts must come back
  with the callback. People who open Tabularia at another name of the same server (an
  IP, an internal alias) or at `http` instead of `https` fail with `tx_missing`.
- **The gateway talks to Keycloak.** It reads Keycloak's configuration, keys and tokens
  at the issuer address, the same one the browser uses: that address must be reachable
  from the cluster, and its certificate signed by an authority the gateway trusts — a
  public one, or the company's own given in `oidc.caCert`; or accepted on purpose
  ([a certificate the gateway does not trust](#a-certificate-the-gateway-does-not-trust)).

Below, Tabularia is at `https://tabularia.example.com` and Keycloak's realm at
`https://sso.example.com/realms/acme`: put your own addresses.

## 1. The client in Keycloak

In the Keycloak admin console, realm `acme`:

1. **Clients → Create client.** Client type *OpenID Connect*, Client ID `tabularia`.
   **Next.**
2. **Client authentication: On** (a confidential client: the gateway keeps a secret).
   Authentication flow: **Standard flow** only; turn off *Direct access grants*.
   **Next.**
3. **Valid redirect URIs:** `https://tabularia.example.com/api/auth/sso/callback`,
   exactly (scheme, host, `/api` prefix, no trailing slash). *Web origins* can stay
   empty: the browser never calls Keycloak's APIs, the gateway does. **Save.**
4. **Advanced → Advanced settings → Proof Key for Code Exchange Code Challenge Method:
   S256.** Tabularia always sends it; this makes Keycloak require it. **Save.**
5. **Credentials:** copy the **Client Secret**.

### Groups or roles

Tabularia matches names: the Keycloak group or role `finance` lands the person in the
Tabularia group `finance`. Choose what carries them.

**Keycloak groups** (people are members of groups in the realm): give the client a
mapper that puts them in the token.

1. **Clients → tabularia → Client scopes → tabularia-dedicated → Configure a new mapper
   → Group Membership.**
2. Name `groups`, **Token Claim Name `groups`**, **Full group path: Off** (otherwise a
   subgroup arrives as `parent/finance`), **Add to ID token: On**. **Save.**

**Keycloak roles** (realm roles or the client's own roles): nothing to add in Keycloak.
Its default *roles* scope already puts them in the access token, where Tabularia reads
them; you only name their place in step 2.

## 2. Tabularia's values

In the values file you install the chart with:

```yaml
oidc:
  enabled: true
  issuer: "https://sso.example.com/realms/acme"
  clientId: tabularia
  redirectUri: "https://tabularia.example.com/api/auth/sso/callback"
  postLoginUrl: "https://tabularia.example.com/auth/callback"
  buttonLabel: "Sign in with ACME"
  # where the groups are (pick one, or several comma-separated):
  #   groups                              Keycloak groups, with the mapper above
  #   realm_access.roles                  realm roles
  #   resource_access.tabularia.roles     roles of the client "tabularia"
  groupsClaim: "groups"
  # the Keycloak group (or role) whose members are Tabularia administrators
  superuserGroup: "tabularia-admins"
  # who may sign in at all: without these, anyone the realm authenticates gets an account
  allowedEmailDomains: "example.com"
  groupAllowlist: ""
  requireAllowlistedGroup: false
  # only when Keycloak's certificate is not signed by a public authority: see below
  caCert: ""
  caConfigMap: { name: "", key: "ca.crt" }
  verifyTls: true
secrets:
  oidcClientSecret: "<the Client Secret of step 1.5>"
```

`issuer` is the realm's address as Keycloak publishes it: open
`https://sso.example.com/realms/acme/.well-known/openid-configuration` and copy its
`issuer` field. If it shows an internal name instead, Keycloak's hostname setting
(`KC_HOSTNAME`) is not the public address: fix that first, the browser and the gateway
must use the same one.

Apply it:

```bash
helm upgrade <release> infrastructure/helm/tabularia --namespace <namespace> \
  --values values-production.yaml --wait --timeout 20m
```

### A certificate the gateway does not trust

When Keycloak's certificate is signed by the company's own certificate authority (or is
self-signed), the gateway does not trust it and every sign-in fails with
`idp_untrusted`. Two ways out; the first is the one to prefer.

**Give the gateway the company's authority** (`OIDC__CA_FILE`, chart `oidc.caCert` or
`oidc.caConfigMap`). From a file:

```bash
helm upgrade … --set-file oidc.caCert=acme-root-ca.crt
```

or name a ConfigMap that already holds it, such as a bundle trust-manager distributes:
`oidc.caConfigMap: { name: acme-bundle, key: root-certs.pem }`. The public authorities
stay trusted, and the certificate is still checked: only Keycloak's real one is
accepted. To see which authority signed it:
`openssl s_client -connect sso.example.com:443 -showcerts </dev/null` (the last
certificate of the chain, or its issuer).

**Accept the certificate without checking it** (`OIDC__VERIFY_TLS=false`, chart
`oidc.verifyTls: false`), when the authority cannot be had. Whether to accept it is the
installer's decision, and it has a cost: the gateway takes from Keycloak, on that same
connection, the keys it checks the sign-ins with. Whoever can stand between the cluster
and Keycloak — a proxy, a tampered name server — can then hand it keys of their own and
sign in as anyone, administrators included. Acceptable on a test cluster, or on a network
where nobody else can reach that path; elsewhere give the authority instead. The gateway
logs a warning at every start while it is off, and a CA file given with it is not used.

### The groups in Tabularia

Create, in **Administration → Groups**, the groups whose names Keycloak sends, and
give them folders and permissions as for any group. A Keycloak group or role with no
Tabularia group of that name is ignored, unless `oidc.autoCreateGroups` is `true`.
Keycloak adds a few roles of its own (`offline_access`, `uma_authorization`,
`default-roles-acme`): they match no group, and are ignored.

The full list of settings, with what each does: [configuration reference](configuration.md),
section *Single sign-on (OIDC)*. How names, administrators and observers are mapped, and
why: [SSO and group mapping](design/sso-group-mapping.md).

## 3. Check

```bash
curl -s https://tabularia.example.com/api/auth/sso/config
```

answers `{"enabled":true,"button_label":"Sign in with ACME"}`. Sign in with a person of
the realm: Tabularia opens, signed in. In **Audit** the sign-in is there (*SSO login*),
with the groups it received, added and removed.

From then on every sign-in sets the groups again (`oidc.authoritative`, on by default):
someone removed from a Keycloak group loses the Tabularia group at the next sign-in.
Administrator and observer groups made in Tabularia are never changed by Keycloak; an
administrator comes from Keycloak only through `oidc.superuserGroup`.

## When it fails

A failed sign-in comes back to Tabularia's sign-in page with a short explanation and a
code. The gateway's log has the whole reason (`kubectl logs deployment/<release>-tabularia-gateway`),
and the audit log the code (*Failed SSO login*).

| Code | What it means, what to do |
|---|---|
| `tx_missing` | The sign-in came back without its cookie: it started at an address other than the redirect URI's (another name, an IP, `http` against `https`). The gateway logs `sign-in started at <address> but OIDC__REDIRECT_URI is on <address>` when it begins. Have people open Tabularia at the redirect URI's address, or register theirs too |
| `tx_expired` | Longer than 10 minutes between the click and the return (`OIDC__LOGIN_TX_TTL_SECONDS`: a first sign-in that asks to set a password or a second factor can take that), or the clocks of the cluster's nodes more than a minute apart: check NTP |
| `state_mismatch` | The answer is not for this sign-in (an old or altered link): start again. Two sign-ins at once — a double click, a second tab — are fine |
| `token_invalid` | The id_token did not pass the checks. Usually Keycloak's clock more than a minute apart from the cluster's (check NTP on both), or an `issuer` that differs from the one Keycloak signs with |
| `code_rejected` | Keycloak refused to exchange the code: a wrong client secret, or a redirect URI not identical to the registered one |
| `idp_untrusted` | Keycloak's certificate is signed by an authority the gateway does not trust: a company one, or none. Give the authority in `oidc.caCert` or `oidc.caConfigMap`, or accept the certificate with `oidc.verifyTls: false` ([a certificate the gateway does not trust](#a-certificate-the-gateway-does-not-trust)). The gateway log names the host and the reason |
| `idp_unreachable` | The gateway cannot reach Keycloak at the issuer address: name resolution or network policy from the cluster (with `networkPolicy.egressCIDRs` restricted, allow Keycloak's address) |
| `idp_denied` | Keycloak did not let the person in (disabled account, a required action refused) |
| `email_unverified` | An account with that e-mail exists in Tabularia and Keycloak does not say the address is verified: tick *Email verified* on the user, or have them verify it |
| `not_allowed` | The person is outside `allowedEmailDomains`, or without a group of `groupAllowlist` while `requireAllowlistedGroup` is on |

A person who signs in but lands without groups: their token does not carry them. Check
the mapper (step 1, *Add to ID token* on) or the place named in `groupsClaim`; the audit
entry of the sign-in lists the groups received.

A runnable example with a ready realm, to try all this on a laptop with Docker Compose:
[Keycloak example](examples/keycloak/README.md).
