# Security policy

Tabularia holds the passwords of your databases, the keys of your buckets and the data
that flows through them. A security problem in it matters, and reports are welcome.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately through GitHub instead:
[**Report a vulnerability**](https://github.com/JumpAsAService/Tabularia/security/advisories/new)
(the repository's *Security* tab → *Report a vulnerability*). Only the maintainers see it.

Useful to include:

- the version (the settings menu shows it, or the image tag) and how it is deployed
  (Docker Compose, Helm on Kubernetes, minikube, kind);
- what an attacker can do, and what they need first (an account? which role? network access?);
- the steps to reproduce it, ideally on the sample data;
- whether it is already known or published somewhere.

What happens next:

- an acknowledgement within **5 working days**;
- a first assessment (confirmed or not, severity) within **14 days**;
- a fixed release as soon as it is ready, on the long-term support line first
  ([docs/lts.md](docs/lts.md)), then a public advisory that credits you, unless you prefer
  otherwise.

Tabularia is maintained by a small team in its free time: these are targets, not
contractual terms. Please give us a reasonable time to ship a fix before you disclose.

## Supported versions

| Version | Security fixes |
|---|---|
| 1.4.x (long-term support, image tag `lts`) | yes, on the newest 1.4.x |
| earlier than 1.4 | no: upgrade to the newest 1.4.x |

Fixes are released as a new patch version, never by changing an image that was already
published: pin an exact version, and move to the next one when it comes out.

## Scope

In scope: the code of this repository (the engine, the gateway, the AI service, the
interface, the data API pods), its published images and its Helm chart.

Not in scope, or not a vulnerability by itself:

- the **trial stack** of `infrastructure/docker-compose.yml`: it is meant for one
  computer, binds to `127.0.0.1` and uses development credentials (Grafana, MinIO) by design;
- findings that need an administrator account, unless they cross a boundary the
  documentation promises (for example a script escaping its environment, or a secret
  shown back after it was saved);
- denial of service by sheer volume against an installation without the limits the
  documentation describes;
- vulnerabilities in third-party software that Tabularia only runs (PostgreSQL, ClickHouse,
  MinIO, Kubernetes…): please report those upstream.

## Hardening

How to run Tabularia safely in production (secrets, TLS, network policies, the roles of
users and the limits of scripts) is described in
[docs/deploy/kubernetes.md](docs/deploy/kubernetes.md) and in the
[configuration reference](docs/configuration.md#before-production).
