# Spec: share links

Status: approved by the owner on 2026-11-07 (decisions below), implemented on `main`.

## Objective

A person who looks after a pixel-perfect report or a dashboard gives it to **someone outside
Tabularia**: a customer, a supplier, a manager with no account. They make a **link** with a
temporary password and send both; the recipient opens the link, replaces the password with
one of their own, and from then on reads the report or the dashboard with its parameters, as
a person inside Tabularia would, and nothing else. A link can also be **embedded** in another
site (an intranet page, a portal) with no password, only in the pages of the origins its
creator listed.

Readers are the creators (analysts, the owners of a folder), the recipients (anyone, no
training, often on a phone), and the administrators who must be able to see every link
that exists and switch the feature off.

## Decisions (owner, 2026-11-06 and 2026-11-07)

| Question | Decision |
|---|---|
| What can be shared | A pixel-perfect report or a dashboard, one per link |
| Who opens it | Whoever has the link and its password. No account, no SSO, no Tabularia sign-in in front of it: a minimal password page of the link |
| Password | **The link's own**. The creator sets a temporary one; at the first entry the recipient must replace it with their own, which must pass the rules of account passwords. **A link is for one recipient**: the first person who enters chooses the password. For several recipients, several links. The creator (or an administrator) can set a new temporary password at any time; doing so closes the link's open sessions. The recipient's password is never visible to the creator |
| Data | **Live**: the link reads the current data with the **creator's** rights, as the creator would see it now. No snapshot, no pinning, no copy |
| Parameters | Changeable by whoever opens the link, as in the viewer inside the app (choices, cascades, defaults). The creator chooses the values the link opens with |
| Who creates links | Administrators, or whoever has MANAGE on the object's folder |
| Off switch | One switch in the Administration panel turns the feature off: every link stops answering, none is deleted, and they work again when it is turned back on |
| Embedding | An option of each link. An embeddable link asks **no password**: it has a mandatory list of allowed origins (`Content-Security-Policy: frame-ancestors`), it is served **only inside an iframe** (`Sec-Fetch-Dest: iframe`), it has a mandatory expiry, it can be revoked, every opening is audited, and the creation dialog asks for an explicit confirmation that whoever sees the host page sees the data |
| AI comment | Written at every opening by the AI service, with a cap per link and per hour; past it the last comment written for that link is shown |
| Internal networks | An installation reachable only on its network or a VPN can publish the links alone on a host of their own: an optional Ingress of the chart (`share.publicHost`), an example for Caddy in the guide |

Out of this version: sharing a flow, a datasource, a SQL script or a notebook; links that
open several objects; a recipient's account made from a link; email sent by Tabularia
with the link (the creator sends it); comments by the recipient; a public link with no
password and no embedding; MCP tools on links.

## Implementation

### What the creator gets

The **Share** button in the viewer of a report and of a dashboard (with MANAGE on the folder,
or as an administrator) opens a dialog:

- **New link**: a label (who it is for, up to 200 characters), the expiry (a date, mandatory,
  at most `max_days` ahead), the values of the parameters it opens with (the viewer's of
  the moment, editable), the downloads allowed (a report: PDF, Excel; a dashboard: PDF,
  image; none by default), and one of two modes:
  - **With a password** (the default): a temporary password, typed or generated (16 random
    characters from an alphabet without look-alikes), at least as strong as an account's.
  - **Embeddable**: the allowed origins (1 to 10, each `https://host[:port]`, or `http://`
    for an intranet name; no path, no wildcard), and a checkbox that must be ticked:
    *«Anyone who can see the pages of these sites will see this data, with my rights.
    The link asks no password.»*
- After **Create**: the link (and, embeddable, the `<iframe>` snippet), with a Copy button.
  **It is shown once**: the gateway keeps only its hash, as for API tokens. The temporary
  password is not shown again either.
- **The links of this object**: label, mode, created by, created, expires, last opened,
  openings, state (*waiting for the first entry*, *active*, *locked*, *expired*, *revoked*,
  *suspended*), with **New temporary password** and **Revoke**.

The Administration panel has a **Share links** tab: the switch, the two limits, and every
link of the installation (with the object and its folder), revocable from there.

### The model (migration 0039)

`share_links`:

| Column | |
|---|---|
| `id` | serial |
| `token_hash` | sha-256 of the token, hex, unique. The token is 32 random bytes (256 bits) in base64url: 43 characters; never stored |
| `token_hint` | its first 4 characters, to recognise a link in a list |
| `kind`, `object_id` | `report` or `dashboard`, and its id (no foreign key: two tables; a link whose object is gone answers as unknown and is purged) |
| `label` | up to 200 characters |
| `creator_id` | `users.id`, `ON DELETE CASCADE`: a deleted creator takes their links with them |
| `embeddable`, `origins` | the mode; the origins as a JSON list (empty for a link with a password) |
| `password_hash` | bcrypt (`internal/passwords`); NULL for an embeddable link |
| `password_temporary` | true until the recipient chose theirs |
| `failures` | consecutive wrong passwords; 50 lock the link until a new temporary password |
| `params` | JSON, the values it opens with, as `given` (`{name: [texts]}`) |
| `downloads` | JSON list: `pdf`, `xlsx` for a report; `pdf`, `png` for a dashboard |
| `expires_at`, `revoked_at` | naive UTC as the rest |
| `created_at`, `last_opened_at`, `openings` | |
| `ai_hour`, `ai_calls`, `ai_last` | the AI cap's hour, its count, and the last comments written (JSON: comments, reading, parameters, time) |

`share_link_sessions`: `id` (sha-256 of a random 32-byte secret), `link_id` (cascade),
`purpose` (`view` or `change`: the few minutes between a correct temporary password and the
new one), `created_at`, `last_seen_at`, `expires_at`, `ip`. Removing the rows closes the
sessions, as `user_sessions`.

`share_link_settings`: one row (`id = 1`): `enabled` (true), `max_days` (90; 1 to 365),
`ai_comments_per_hour` (20; 0 to 200, 0 = no AI comment on links), `updated_at`,
`updated_by`. A table of its own rather than items of Administration → Settings: those are
compared with the Python gateway's, which is no longer extended.

Additions only: three tables the previous version neither writes nor reads. SQLModel models
in `gateway/app/models/share_link.py` only so that «migrated database = models» keeps
holding; no Python route uses them. `SchemaRevision` becomes `0039`.

### With whose rights

- Every request of a link is served as **its creator now**: the creator's account, groups and
  rights at that moment, through the same code as the viewer (`perm.For(creator)`, the
  reports' resolver, the dashboards' numbers and cache). A dataset the creator may not read
  refuses in its tile, as for the creator.
- The link answers only while: the feature is on; the link is not expired, revoked or
  locked; the creator's account is active; the creator is still an administrator or has
  MANAGE on the object's folder; the object exists. Otherwise it is **suspended** (or gone),
  and it says only *«This link is not available»*, the same words for every case, with 404.
  A suspended link comes back by itself when the reason goes (MANAGE given back, feature
  on).
- A deleted creator deletes their links (the foreign key) and their sessions with them.
- What the requests do is what the viewer allows, never more: no SQL and no dataset's source
  leaves the gateway; the definition is sent as a **projection**, everything the viewer draws
  with (layout, items, tiles, parameters) and of its datasets only their names; the render's
  state and clicks are checked as the viewer's.
- The page is the app's own viewer (`pages/report.vue`, `pages/dashboard.vue`) **hosted** by
  the link's page (`composables/viewerHost.ts`): the same component over the link's routes,
  without what belongs to the app (versions, designer, folders, the API link). Clicks on a
  dashboard's tiles work as in the viewer.
- The audit's actor is the creator, with `from: "share_link"` and the link's id in the detail,
  so a reader of the audit knows it was not the creator in person.

### The addresses

- The page: `/share/<token>` on the interface (Nuxt). The API: `<API prefix>/share/<token>/…`
  on the Go gateway (behind Caddy and the ingress: `/api/share/<token>/…`). **No change to the
  proxy or the ingress**: `/api` already goes to the gateway, everything else to the interface.
  A separate prefix (`/s/`) would need a rule in Caddy and in the chart and buys nothing.
- The token is in the path, never in a query string (which ends up in more logs); the gateway
  writes no access log of paths, and every log line of these routes names the link's id,
  never its token.

### The public API (Go only, `server/go_only_routes.go`)

All answer `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy:
no-referrer`. An unknown, expired, revoked, locked or suspended link: 404 *«This link is not
available»*. Every POST needs `X-Tabularia-Csrf: 1`, as the app's.

| Route | |
|---|---|
| `GET /share/{token}/frame` | For the interface's server only: `{embeddable, frame_ancestors}` (`'none'` for a link with a password or an unknown one). No session, no audit |
| `POST /share/{token}/session` | A link with a password: `{password}`. Right and temporary: `{change_password: true}` and a `change` session (10 minutes). Right: a `view` session. Wrong: 401, counted. An embeddable link: no body, a `view` session returned in the body; audited as an opening |
| `POST /share/{token}/password` | With a `change` session: `{password}`, the recipient's own. Checked with the account rule (`gatewaymodels.PasswordProblem`: at least 12 characters, at most 72 bytes, not a known one) and different from the temporary one. One wins if two try at once: the update is conditional on the password still being temporary |
| `GET /share/{token}` | What the page shows: kind, name, locale, the projection, the values it opens with, the downloads allowed, whether it has AI comments |
| `GET /share/{token}/parameters` | The form, as `/reports/{id}/parameters` and `/dashboards/{id}/parameters` |
| `POST /share/{token}/data` | A dashboard's numbers, as `/dashboards/{id}/data` |
| `POST /share/{token}/render` | A report's pages on screen (`svg`), or a download allowed; a dashboard's PDF or PNG if allowed. Answers a handle of the export, bound to the link (an HMAC of the link and the task with the gateway's secret) |
| `GET /share/{token}/exports/{handle}` and `…/file` | Its state and its file, as `/tasks/exports/{id}` |
| `POST /share/{token}/comment` | The AI comments of the document with these values (and a dashboard's clicks), under the cap |

The sessions: a link with a password keeps it in the cookie `tab_share`, `HttpOnly`,
`SameSite=Strict`, `Secure` on HTTPS, `Path=<API prefix>/share/<token>` (the browser sends it
to that link's API only, and the gateway reads it only on these routes); 12 hours at most, 60
minutes without requests. An embeddable link cannot rely on a cookie (third-party cookies are
blocked in iframes): the page keeps its session in memory and sends it as
`X-Tabularia-Share-Session`; same lifetimes; a reload opens a new one. The app's own cookie,
sent along to `/api/…`, is never read by these routes; the link's opens no other route.

### The brake on passwords

- Per address and link, `loginthrottle` as for sign-ins (the e-mail column holds
  ` link:<id>`, which no stripped e-mail can be): 5 errors in 5 minutes block that address
  on that link for 60 s, doubling up to 15 minutes; the per-address count (30) is the one
  sign-ins share, so a person trying many links is braked too.
- Per link, whatever the address: `failures`; at 50 the link is **locked** until the creator
  sets a new temporary password. Audited (`share_link.locked`).
- A wrong password costs a bcrypt comparison even when the link is unknown (against a fixed
  hash), so the time does not say which tokens exist.

### Embedding

- The interface's server, for `/share/<token>`, asks the gateway `GET /share/{token}/frame`
  (`NUXT_GATEWAY_INTERNAL_URL`, the gateway's address inside the installation: compose
  `http://gateway:8000`, the chart its service) and writes the page's
  `Content-Security-Policy` with `frame-ancestors` of the link: `'none'` for a link with a
  password, its origins for an embeddable one. If the gateway does not answer: `'none'`.
- An embeddable link's page is served only when the browser says it is loading it in a frame,
  `Sec-Fetch-Dest: iframe`. `document`, or no header, gets a neutral page (*«This link is not
  available»*), with 404. **The choice for browsers that do not send the header**: refused.
  Chrome and Edge send it since version 80 (2020), Firefox since 90 (2021), Safari since
  16.4 (2023); an older browser cannot open an embedded link. The header is a statement of
  the browser, which pages cannot forge but a script outside a browser can: it keeps people
  from opening the address in a tab, it is not a lock. **The real boundary of an embeddable
  link is the host page**: whoever sees it can read the data, and the dialog says so.
- The page writes `<meta name="referrer" content="no-referrer">` too: Caddy adds its own
  `Referrer-Policy` to every answer, and the meta holds whatever the proxy does.
- A browser that is not in an allowed origin's page refuses to show the frame
  (`frame-ancestors`), which the browser tests check on Chrome and Firefox.

### The AI comment

- At each opening (and at **Apply** of new values), the page asks `POST /share/{token}/comment`;
  the gateway asks the AI service's `/internal/ai/comments` with the creator as `user_id`
  (on their bill and with their rights, as a flow group's Pushmail cell does), for the whole
  document. The AI service's own cache (`ai_comments`, by the numbers) makes a second opening
  on the same numbers free.
- **The cap: `ai_comments_per_hour` per link (20 by default)**, counted in `ai_calls` within
  `ai_hour`. Past it the gateway does not call: it answers `ai_last`, the last comments the
  link was given, with their values and time, and the page says *«Written at 10:42 for
  Region = North»* under it. With no comment yet, the element says the comment is not
  available now. The same cap holds for a downloaded PDF.
- A report's pages with a comment element are rendered with the comments the gateway got
  (the engine's `comments` and `reading`), so the screen and the PDF say the same text.

### Lifecycle and audit

- Audit events: `share_link.create`, `share_link.revoke`, `share_link.password_reset` (by
  the creator or an admin), `share_link.password_set` (by the recipient; never the password),
  `share_link.open` (a new session, with its mode), `share_link.unlock_failed`,
  `share_link.locked`, `share_link.refused_frame` (an embeddable link asked as a page),
  `share_link.settings`; the renders as `report.render` / `dashboard.render` with `from:
  "share_link"`. No token, no password, no session in any of them; the address is the
  audit's usual one.
- The orchestrator's chores, every 5 minutes: delete the sessions past their expiry and the
  sessions of expired or revoked links; delete links expired or revoked more than 30 days
  ago, and links whose object no longer exists.
- Revoking deletes the link's sessions at once.

### Exposing the links outside the internal network

- The chart's `share.publicHost` (empty by default) adds an Ingress on that host with only:
  `/share` and `/_nuxt` (Prefix, the interface), `<apiPrefix>/share` (Prefix, the gateway: a
  Prefix matches whole segments, so `/api/share-links` is not in it), and the five icons the
  page names (ImplementationSpecific: ingress-nginx refuses a dot in an Exact path). Everything
  else of that host is not routed: the controller's 404.
- TLS as the main Ingress: with `ingress.tls`, an entry for the host with `share.tlsSecretName`
  (default `<release>-share-tls`); the annotations are the main Ingress's (cert-manager's
  among them) plus `share.annotations`, where a rate limit of the controller goes
  (ingress-nginx `limit-rps`, `limit-connections`), documented as optional.
- The chart refuses a public host that is one of `ingress.hosts`.
- `SHARE__PUBLIC_BASE_URL` (the chart sets it from the host, `https` with TLS) is the address
  the **Share** dialog writes the links with; the gateway gives it to the dialog.
- For Caddy (the production compose, not in the repository): a second site with the same
  paths, in the guide.
- The interface's server reaches the gateway at `NUXT_GATEWAY_INTERNAL_URL` (the chart sets
  it to the gateway's Service; the production image defaults it to `http://gateway:8000`).
  Without it, links with a password work and embeddable ones do not open.

### The page

- `pages/share/[token].vue`, free of the sign-in middleware, with no app chrome: the name, the
  parameters bar, the report's pages (`ReportPages`) or the dashboard's grid
  (`DashboardGrid`, clicks included), the downloads allowed, the AI comment, a small
  *Tabularia* mark. Its own fetch (no redirect to `/login` on a 401: the password form comes
  back). The app's themes and the five languages: the browser's language unless the link's
  object says its own locale, the theme from `prefers-color-scheme`.
- The password page: one field, *Open*. At the first entry: two fields (new password,
  again), the rule said before typing, the errors said in words.
- Errors in the page's words: wrong password (and how long to wait when braked), not
  available, locked (*ask whoever sent you the link*).

## Commands

```bash
# the Go gateway (unit and database tests on a disposable PostgreSQL)
gateway-go/scripts/test.sh
# the Python gateway's suite (the migration and its models)
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
# the routes still answered like Python's
DIFF_NO_PYTHON=1 gateway-go/scripts/diffcheck.sh
# frontend
cd frontend && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
# a stack of one's own for the browser tests (never :8088)
docker compose -p share -f infrastructure/docker-compose.yml -f <override with the Go image> up -d
```

## Project structure

```text
gateway/app/db/migrations/versions/0039_share_links.py   the migration
gateway/app/models/share_link.py                         its models (the schema check only)
gateway-go/internal/sharelinks/                          tokens, sessions, rules, the brake, settings
gateway-go/internal/api/share_links.go                   creating, listing, revoking; the admin tab
gateway-go/internal/api/share_public.go                  the public routes
gateway-go/internal/api/jobs_scheduler_chores.go         the sweep
gateway-go/internal/server/go_only_routes.go             the routes declared
frontend/pages/share/[token].vue                         the public page
frontend/components/share/                               the dialog, the list, the password form
frontend/composables/useShareLinks.ts                    the API and pure helpers
frontend/server/plugins/csp.ts                           frame-ancestors per link
docs/capabilities/share-links.md                         the user's guide
```

## Code style

As the code around it: short comments that say why; names in English; tests named as
sentences. The public handlers are thin: find the link, check the session, then call the
viewer's own handler as the creator.

```go
func TestTheLinksCookieOpensNoOtherRoute(t *testing.T) {
	// the share session is a capability of one link, not of the creator's account
}
```

## Testing strategy

| Level | What |
|---|---|
| Go, unit | Token and session hashing; origins' validation; the password rule; the export handle's HMAC; the projection of a dashboard (no `datasets`, no SQL) |
| Go, database | Create with and without MANAGE, as admin, with the feature off; wrong, expired, revoked, locked, unknown tokens; the temporary password replaced once, a weak password refused, a race of two; a reset closing the sessions; the brake per address and per link; the creator losing MANAGE or deactivated; the cookie opening no other route and the app's cookie opening no link; headers on every answer; the audit of each event; the sweep; the AI cap |
| Python | The migration on real data, «migrated database = models» |
| Diffcheck | 0 differences on the existing routes with `DIFF_NO_PYTHON=1` |
| Frontend | Helpers (origins, password rule, iframe snippet), typecheck, five languages |
| Browser, Chrome and Firefox | Create a link from a report and a dashboard; open it in a clean browser; first entry and new password; parameters; download; revoke; an embeddable link in a host page of an allowed origin (shown) and of another origin (blocked by the browser), and opened as a page (refused); screenshots looked at |
| Chart | `helm lint` and `helm template` with and without `share.publicHost`; on kind, with curl on the public host, the share paths answer and everything else is a 404, and a link opens from there |

## Boundaries

- **Always**: the creator's rights checked at every request; the link's checks before
  anything else; no token, password or session in a log or an audit row; five languages;
  the docs in English and no competitor named.
- **Ask first**: a new dependency; a link that opens more than one object; anything that
  sends e-mail.
- **Never**: the definition or a dataset's source to the public page; a link's session
  accepted by any other route; a route in the Python gateway; pushing or tagging.

## Success criteria

1. An administrator, or someone with MANAGE on the folder, makes a link from the viewer in
   under a minute; someone with only EDIT does not see **Share**.
2. In a browser with no Tabularia session the link asks for the password; the temporary
   password leads to choosing one's own; a weak one is refused in words; then the report or
   the dashboard works with its parameters and allowed downloads, with the creator's data.
3. The cookie of a link opens no other route of the gateway (401), and no other link.
4. Wrong, expired, revoked tokens all say «not available»; the sixth wrong password from one
   address waits; the fiftieth on a link locks it.
5. An embeddable link opens in a page of an allowed origin, is blocked by Chrome and Firefox in
   a page of another origin, and is refused opened as a page.
6. Every answer has `no-store`, `noindex`, `no-referrer`; every opening and failure is in the
   audit.
7. With the switch off no link answers; turned on again they work.
8. The AI comment is written at the opening, and past the cap the last one is shown with its
   time and values.
9. On kind, the public host answers only the share paths, and a link opens from it.
10. All suites green; the diffcheck at 0 differences.

## Open questions

- Safari before 16.4 cannot open an embedded link (no `Sec-Fetch-Dest`): accepted, or a
  fallback?
- Should a recipient be able to sign out of a link (closing their session) from the page?
  Not in this version.
