# Release process

How a release goes from a tag to Docker Hub. Written for whoever maintains the
project; installing or upgrading is in [deploy/release-checklist.md](deploy/release-checklist.md).

## Cutting a release

1. Set the new version everywhere the code carries it: `backend/pyproject.toml`,
   `gateway/pyproject.toml`, `gateway/app/core/config.py` (the version the app shows),
   `frontend/package.json`, and in the Helm chart `appVersion` in `Chart.yaml` plus the
   image tags in `values.yaml` and `values-production.example.yaml`. The workflow
   refuses a tag that disagrees with any of them and names the file.
2. Move the `Unreleased` section of `CHANGELOG.md` under the new version.
3. Commit on `main`, tag and push the tag:

   ```bash
   git tag -a v1.2.0 -m "release: 1.2.0"
   git push origin main v1.2.0
   ```

The tag starts [`.github/workflows/release-images.yml`](../.github/workflows/release-images.yml).

## What the workflow does

1. **Version and tags.** Checks the tag against the version in the code, works out
   which tags to publish, and runs the tests of the secret scanner.
2. **One job per image and architecture** (backend, gateway, frontend × amd64,
   arm64; arm64 runs on GitHub's native ARM runners, no emulation). Each job:
   - builds the image from scratch on fresh base images;
   - smoke-tests it: runs as uid 10001, has the right architecture and version
     label; the engine has no chDB, the gateway imports its migrations, the
     interface answers `/login` with a read-only filesystem;
   - saves it with `docker save` and **scans it for secrets**, twice (below);
   - only then logs in to Docker Hub and pushes `1.2.0-amd64` or `1.2.0-arm64`. The
     Docker Hub token never enters a job whose scans have not passed.
3. **Multi-architecture tags.** Joins the two architectures under `1.2.0`, under
   `1.2` when it is the highest `1.2.x`, and under `latest` when it is the highest
   stable release: moving tags only move forward. A pre-release tag
   (`v1.2.0-rc.1`) publishes its own name only.

**Dry run:** Actions → *Release images* → *Run workflow*, give an existing tag and
leave *publish* off: everything runs except the pushes.

## The two secret scans

**Your secrets, by fingerprint** (`.github/scripts/image_secret_scan.py`). CI must not
hold the maintainer's credentials, so it holds fingerprints instead: for each secret
its length, 32 bits of its first 8 bytes and its SHA-256. The scanner opens the image
one layer at a time, including files a later layer deletes, the image config with its
environment and history, and zip, gzip and tar archives inside it (three levels deep),
and flags any byte sequence whose fingerprint matches. Nothing is printed but the name
of the secret and where it was found.

**Anybody's live credentials** (TruffleHog, pinned by digest). Any credential it can
verify as live stops the release. For cloud providers and the services this project
uses (AWS, Google, Azure, Scaleway, GitHub, Docker Hub, OpenAI, Anthropic…) a
credential it *cannot* verify, for instance because of a network error, stops it too.
The log shows only the kind of credential and the file: the public Actions log never
carries the value.

## Before pushing: the local guard

The scans above stop a secret on its way to Docker Hub, but a secret committed by
mistake is public as soon as the commit reaches GitHub. The pre-push guard
(`.github/scripts/push_guard.py`) stops it earlier, on the maintainer's machine, on the
commits the remote does not have yet:

- **your secrets, by value**, in the lines and commit messages the push adds, binary
  files included. The values stay on the machine, so values from 10 characters are
  checked, shorter than the fingerprints allow;
- **TruffleHog** on the same commits, with the workflow's rules.

A secret the remote already has does not stop the push, so that removing it stays
possible; it is reported. Install it once per clone and tell it where your secrets are
(local git config, never committed):

```bash
printf '#!/bin/sh\nexec python3 "$(git rev-parse --show-toplevel)/.github/scripts/push_guard.py" "$@"\n' \
    > .git/hooks/pre-push && chmod +x .git/hooks/pre-push
git config --add tabularia.secretFiles infrastructure/.env
git config --add tabularia.secretFiles ~/path/to/service-account.json
git config tabularia.secretStandard true      # cloud tools' credential files and SSH keys
```

It needs Docker for TruffleHog; `git push --no-verify` skips it in an emergency.

## Repository secrets

| Secret | What | Renewal |
|---|---|---|
| `DOCKERHUB_TOKEN` | Docker Hub personal access token of `jumpasaservice`, permission *Read & Write* | it expires (one year): generate a new one in Docker Hub → Account settings → Personal access tokens |
| `SECRET_FINGERPRINTS` | fingerprints of the maintainer's secrets | whenever a secret changes or a new one appears |

Without `SECRET_FINGERPRINTS` the scan cannot certify anything and the release stops.

### Generating the fingerprints

On the maintainer's machine, never in CI:

```bash
python3 .github/scripts/image_secret_scan.py fingerprint --standard -o /tmp/fingerprints.txt \
    infrastructure/.env infrastructure/production/.env.prodtest path/to/service-account.json
```

`--standard` adds the credential files of cloud and developer tools when present
(`~/.aws/credentials`, gcloud application-default credentials, `~/.config/scw`,
`~/.docker/config.json`, `~/.kube/config`, `~/.netrc`, `~/.git-credentials`, the GitHub
CLI, OCI, doctl, hcloud) and the private SSH keys in `~/.ssh`. From `.env`-style and YAML
files it takes the values whose name says secret (`KEY`, `SECRET`, `PASSWORD`, `TOKEN`,
`CREDENTIAL`, `DSN`, `PRIVATE`), passwords inside URLs, and decodes base64 values that
hide a service-account JSON or a PEM key; a PEM key is fingerprinted line by line.

Paste the file into the secret (Settings → Secrets and variables → Actions →
`SECRET_FINGERPRINTS`) and delete it. The command reports, by name only:

- values **already written in the repository**, left out because they are public —
  usually the defaults of a `.env.example`; a real secret in that list is already
  public and must be rotated;
- values **shorter than 16 characters**, not fingerprinted: a fingerprint would let
  anyone recover them by brute force. They are still checked against the repository,
  which is the one way a secret reaches an image built in CI.

## Limits

- Archives nested deeper than three levels, or compressed with something other than
  zip or gzip, are scanned as raw bytes only.
- A secret stored in the image in a different encoding from the one it has on the
  maintainer's machine (base64 of a raw value, for instance) does not match its
  fingerprint; TruffleHog still recognises the usual cloud key formats.
- The single-architecture tags (`1.2.0-amd64`, `1.2.0-arm64`) stay on Docker Hub
  next to the joined ones.
