# Share links

A **share link** gives a pixel-perfect [report](reports.md) or a [dashboard](dashboards.md) to
someone outside Tabularia: a customer, a supplier, a manager with no account. They open the
link, type a password, and read the report or the dashboard **live**, with its parameters, as
you would see it now. Nothing else of Tabularia is open to them: no folder, no flow, no other
report, no sign-in page.

A link can also be **embedded** in the pages of another site, such as an intranet or a portal,
with no password, only inside the sites you list.

## At a glance

| | |
|---|---|
| Who makes links | An administrator, or whoever has **MANAGE** on the object's folder |
| What a link opens | One report or one dashboard |
| With whose rights | **Yours, now**: the data are read at every opening with your current rights |
| Password | A temporary one you choose; the recipient replaces it with their own at the first entry |
| One link, one person | The first person who enters chooses the password: for several people, make several links |
| Parameters | The recipient can change them, as in the viewer |
| Downloads | None by default; PDF and Excel (a report), PDF and image (a dashboard) if you allow them |
| Expiry | Always; at most 90 days by default (Administration → Share links) |
| Embedding | An option of each link: no password, only in the pages of the sites you list |
| Off switch | Administration → Share links: off, no link answers; on again, they all work |

## Making a link

1. Open the report or the dashboard in its viewer and set the parameters it should open with.
2. Click **Share** (it is there only for an administrator, or with MANAGE on the folder).
3. Write who it is for (a label only you see), choose **With a password**, keep the generated
   temporary password or write one (at least 12 characters), choose the expiry and the
   downloads allowed, and click **Create the link**.
4. Copy the link and the temporary password. **They are shown only now**: Tabularia keeps a
   fingerprint of the link, not the link itself.
5. Send the link and the password **through two different channels**: the link by email, the
   password by a message or on the phone.

The dialog lists the links of the object: who made each, its expiry, how many times it was
opened and when last, and its state (*waiting for the first entry*, *active*, *locked*,
*expired*, *revoked*, or *suspended* with the reason). From there:

- **New temporary password**: the recipient lost theirs, or the link should go to someone
  else. Every open session of the link is closed at once; whoever opens it next chooses a
  new password.
- **Revoke**: the link stops working for good.

## What the recipient sees

1. The link opens a small page asking for the password: nothing of Tabularia around it.
2. The first time, after the temporary password, they choose their own (at least 12
   characters, not a well-known one, typed twice). From then on the link opens only with it;
   **you never see it**.
3. Then the report's pages, or the dashboard's tiles, with the parameters bar, the downloads
   you allowed and, if the object has one, its AI comment.

A wrong password says so; after five from the same address the page waits a minute, then
longer, up to 15 minutes. After 50 wrong passwords in a row on the same link, from anywhere,
the link **locks** until you set a new temporary password. A session lasts at most 12 hours,
and ends after an hour with nothing asked. An expired, revoked, locked or suspended link
says only *«This link is not available»*.

## With whose rights

Every request of a link is served **as you, now**: your account, your groups, your rights at
that moment, through the same code as your own viewer. A dataset you can no longer read
refuses in its tile, as it would for you.

A link is **suspended** (it answers *not available*, and comes back by itself when the reason
goes) while:

- your account is deactivated;
- you are no longer an administrator and no longer have MANAGE on the object's folder;
- an administrator switched share links off.

If your account is deleted, your links are deleted with it. If the report or the dashboard
is deleted, its links are removed.

## Embedding a link in another site

Choose **Embeddable** in the dialog, list the sites that may show it (one per line, as
`https://intranet.example.com` or `https://portal.example.com:8443`: no path, no wildcard),
tick the confirmation and create the link. The dialog gives the `<iframe>` to paste in the
host page:

```html
<iframe src="https://tabularia.example.com/share/…" title="Sales overview" width="100%" height="720"
  style="border:0" referrerpolicy="no-referrer"></iframe>
```

An embeddable link:

- **asks no password**. Browsers do not give a frame from another site its cookies, so a
  password could not hold a session there: **anyone who can see the host page sees the
  data, with your rights**. The dialog asks you to confirm it.
- opens **only inside a frame of the sites you listed**: the page tells the browser which
  sites may frame it (`Content-Security-Policy: frame-ancestors`), and Chrome, Edge, Firefox
  and Safari refuse to show it in any other site's page.
- **opens only in a frame**: typed in the address bar, it says *not available*. The browser
  tells Tabularia how it is loading the page (`Sec-Fetch-Dest`); a browser too old to say it
  (Safari before 16.4, Chrome before 80, Firefox before 90) cannot open embedded links.
  This keeps people from opening it on its own; it is not a lock against a program that
  knows the address, which is in the host page anyway.
- always has an expiry, can be revoked, and every opening is in the audit.

## The AI comment

A report or a dashboard with an [AI comment](dashboards.md#ai-comments) writes it at every
opening, and at every **Apply** of new values, on the numbers of that moment, billed to the
creator as their own. Comments already written on the same numbers are reused for free.

So that a link left open, or opened by many people, cannot make unlimited calls, a link may
ask for **20 comments an hour** (Administration → Share links; 0 turns AI comments off on
links). Past that, it shows the last comment written for it, with the time and the values
it speaks of.

## For the administrator

**Administration → Share links** has:

- **Share links are on**: off, no link answers and none can be made; the links stay, and work
  again when it is turned back on.
- **Longest life of a link** (1 to 365 days, 90 by default): the furthest expiry a creator may
  choose.
- **AI comments per link and hour** (0 to 200, 20 by default).
- Every link of the installation, with its object, its creator, its expiry, its openings and
  its state, revocable from there.

The audit log records `share_link.create`, `share_link.revoke`, `share_link.password_reset`,
`share_link.password_set` (never the password), `share_link.open`, `share_link.unlock_failed`,
`share_link.locked`, `share_link.refused_frame` and `share_link.settings`, and the renders as
`report.render` and `dashboard.render` with `from_share_link`. No event holds a link, a
password or a session.

What a link's pages send: `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`,
`Referrer-Policy: no-referrer` (the link never leaves in a `Referer`), and their own
`Content-Security-Policy`. A link's session opens no other route of Tabularia, and a
Tabularia session opens no link.

## Exposing the links outside the internal network

On an installation reachable from the internet, the links work with no setting: they are
written with the interface's address. When Tabularia is reachable **only on the internal
network or a VPN**, publish the links alone on a host of their own, reachable from outside.
On that host only these paths answer, and everything else is a 404:

| Path | Goes to |
|---|---|
| `/share/…` | the interface: the link's page |
| `/api/share/…` (the API prefix) | the gateway: the link's routes (not `/api/share-links`, which is the creators') |
| `/_nuxt/…` | the interface: the page's scripts, styles and fonts |
| `/favicon.svg`, `/favicon.ico`, `/favicon-16.png`, `/favicon-32.png`, `/apple-touch-icon.png` | the interface: the icons |

The **Share** dialog then writes the links with that host: the gateway reads it from
`SHARE__PUBLIC_BASE_URL` ([Configuration reference](../configuration.md)).

### With the Helm chart

```yaml
share:
  publicHost: share.example.com
  # tlsSecretName: share-example-com-tls   # with ingress.tls set: the certificate of this host
  annotations:
    # optional, ingress-nginx: requests per second and connections per client address
    nginx.ingress.kubernetes.io/limit-rps: "10"
    nginx.ingress.kubernetes.io/limit-connections: "20"
```

The chart adds a second Ingress (`<release>-share`) on that host with the paths above,
the same class and annotations as the main one (so cert-manager issues its certificate when
`ingress.tls` is set) plus `share.annotations`, and sets `SHARE__PUBLIC_BASE_URL` to
`https://share.example.com` (`http://` without TLS; `share.publicBaseUrl` to write another).
The host must not be one of `ingress.hosts`. Point the public DNS name at the controller's
public address.

To check it, from a computer outside the network:

**Linux and macOS** (Terminal):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://share.example.com/share/x        # 200
curl -s -o /dev/null -w '%{http_code}\n' https://share.example.com/login          # 404
curl -s -o /dev/null -w '%{http_code}\n' https://share.example.com/api/auth/me    # 404
curl -s -o /dev/null -w '%{http_code}\n' https://share.example.com/api/share-links # 404
```

**Windows** (PowerShell):

```powershell
foreach ($p in '/share/x', '/login', '/api/auth/me', '/api/share-links') {
  try { (Invoke-WebRequest -UseBasicParsing "https://share.example.com$p").StatusCode } catch { [int]$_.Exception.Response.StatusCode }
}
```

The first answers 200 (the page, which then says the link is not available), the others 404.

### With Caddy (the production Docker Compose)

Add a second site to the Caddyfile next to the interface's one, and set
`SHARE__PUBLIC_BASE_URL=https://share.example.com` in the environment of the gateway:

```caddyfile
share.example.com {
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		-Server
	}

	# the link's routes only: /api/share/…, never /api/share-links
	handle_path /api/* {
		@share path /share/*
		handle @share {
			reverse_proxy {
				dynamic a {
					name gateway
					port 8000
					refresh 5s
				}
				lb_policy least_conn
				header_up X-Forwarded-For {client_ip}
			}
		}
		handle {
			respond 404
		}
	}

	# the link's page and the files it loads
	@page path /share/* /_nuxt/* /favicon.svg /favicon.ico /favicon-16.png /favicon-32.png /apple-touch-icon.png
	handle @page {
		encode zstd gzip
		reverse_proxy frontend:3000
	}

	handle {
		respond 404
	}
}
```

Caddy gets the certificate of the new name by itself, as for the interface's, once the DNS
points at it. A rate limit is not part of Caddy's standard build; put one in front (a load
balancer, a firewall) if you need it.

The interface's own site sends `X-Frame-Options: SAMEORIGIN` on every page; browsers ignore
it on a link's page, which sends its own `frame-ancestors`, so embedding works. If you prefer
the header not to be there at all, write it in the interface's site as
`?X-Frame-Options SAMEORIGIN` (Caddy then sets it only where the page did not).

## Limits and good to know

- A link opens one object; a folder or a collection of reports cannot be shared.
- Tabularia does not send the link: you do, with your own email.
- A recipient cannot sign out of a link from its page; the session ends by itself (12 hours,
  or an hour without requests), or when you set a new temporary password.
- Clicks on a dashboard's tiles work on a link as in the viewer, and are not saved.

## Related

- [Reports](reports.md) and [Dashboards](dashboards.md)
- [Permissions](../governance/permissions.md)
- [Configuration reference](../configuration.md)
