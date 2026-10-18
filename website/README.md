# Tabularia website

The presentation site and the documentation, published on GitHub Pages at
<https://jumpasaservice.github.io/Tabularia/>. A static [Nuxt](https://nuxt.com) site,
separate from the application in `frontend/`: nothing here goes into the product's images.

- `/` — the landing page. All its texts are in [`app/locales/en.ts`](app/locales/en.ts),
  shaped like a vue-i18n catalogue so `@nuxtjs/i18n` can take it over later.
- `/docs/...` — the documentation, generated at build time by [Nuxt Content](https://content.nuxt.com)
  from the repository's [`docs/`](../docs) folder (read in place, never copied) plus the README as
  the overview. `docs/audit/` and `docs/specs/` are internal and left out. The pages written only
  for the site (Getting started) are in [`content/`](content).

## Run it locally

Node 22.5 or newer (the docs index uses Node's built-in SQLite) and npm 11.21.1, the version pinned
in `package.json` (`packageManager`) and installed by CI. With npm 10 (the one Node 22 ships) a
fresh install crashes in the dependency resolver (`Cannot read properties of null (reading
'edgesOut')`, the bug also noted in `frontend/Dockerfile`), and `npm ci` on this lock file stops on
the optional native packages of other platforms (`EBADPLATFORM`).

```bash
cd website
npx npm@11.21.1 ci        # or: npm install --global npm@11.21.1, then npm ci
npm run dev               # http://localhost:3000/Tabularia/
```

The dev server reloads on changes to the site and to `../docs`.

## Build, check, preview

```bash
npm test                  # unit tests of the landing page's placeholder resolver
npm run generate          # static site in .output/public
npm run check-links       # every internal link and #anchor, and every GitHub link against the repository
npm run preview           # serves .output/public at http://localhost:4173/Tabularia/, as Pages will
npm run clean             # drop .nuxt, .output and .data (the parsed-docs cache)
```

Run `npm run clean` after changing `lib/doc-links.mjs`: Nuxt Content caches parsed pages by the
file's content, so a change to the link rewriting alone is not picked up otherwise.

### Where the site lives

`SITE_URL` is the one value to change when the site moves; the base path is derived from it
([`site.config.mjs`](site.config.mjs)):

```bash
SITE_URL=https://jumpasaservice.github.io/Tabularia/ npm run generate   # the default: under /Tabularia/
SITE_URL=https://tabularia.example.org/ npm run generate                 # a custom domain: at the root
```

For a custom domain, also add `public/CNAME` with the host name and set it in Settings → Pages.

## How the docs are turned into pages

- Routes follow the files: `docs/deploy/kubernetes.md` → `/docs/deploy/kubernetes`; a folder's
  README is the folder (`docs/examples/keycloak/README.md` → `/docs/examples/keycloak`); the
  repository README is `/docs/overview`.
- What is published ([`lib/repo-files.mjs`](lib/repo-files.mjs)): only Markdown that is in git or
  committed together with the site (`docs/configuration.md` and its generator), so a build on a
  working copy and CI on a clean clone produce the same site. Never published: `docs/audit/`,
  `docs/specs/`, `docs/deploy/docker-compose.md` and `infrastructure/production/` (the production
  compose stays local); a link to them, or to any file not on GitHub, becomes plain text, and
  "Edit this page" appears only for files on GitHub.
- On the overview page the README's donation badge (a third-party request) and its product
  GIFs (they still show the logo replaced on 2026-10-18) are left out; `frontend/public/logo.svg`
  is served by the site (`public/logo.svg`).
- Links are rewritten before parsing ([`lib/doc-links.mjs`](lib/doc-links.mjs)): a link to a
  published doc becomes its route, images in `docs/media` and the diagrams in `docs/architecture`
  are served by the site (the README's links to `…/Tabularia/architecture/*.html` keep working),
  any other repository path becomes its GitHub URL.
- Headings get GitHub's ids, so anchors written for GitHub (`#run-it-on-your-engine--or-on-your-warehouse`)
  work on the site too.
- The sidebar groups are in [`app/utils/docs-nav.ts`](app/utils/docs-nav.ts). A new file in `docs/`
  is published even if it is not listed there: it lands under "Reference" until it is placed.
- Search is a static index (`/search.json`, one entry per section) loaded on first use and
  searched in the browser.
- The `raw` / `endraw` Liquid comments at the top and bottom of some pages are for the old Jekyll
  build of GitHub Pages; they are harmless here and are left in place.

## Deploy

[`.github/workflows/website.yml`](../.github/workflows/website.yml) runs on every push to `main`
that touches `website/`, `docs/`, `brand/` or the README, and by hand (Actions → Website → Run
workflow): it installs, runs the unit tests, generates with `SITE_URL`, checks the links, and
publishes the result with the official Pages actions. A pull request touching the same paths gets
the same build and checks, without the deploy. The steps are also in
[docs/release-process.md](../docs/release-process.md#the-website-github-pages).

**Once, before the first deploy:** a repository admin opens Settings → Pages → Build and
deployment and sets **Source** to **GitHub Actions**. Until then the old branch-based Pages build
(Jekyll over the repository) keeps publishing and the workflow's deploy job fails. After the
switch, Jekyll no longer runs, so the `raw` wrappers and the push guard's Liquid check are
not needed for Pages any more; they do no harm and can stay.

## Pictures

- `public/shots/` — three real screenshots of the product: crops of the dev stack with the sample
  data (development-sample setting, datasources) and of `docs/media/openlineage-marquez.png` (the
  graph only). Each was checked for error states, test leftovers and the old logo. The rest of the
  product on the page (the flow joining SFTP, S3 and Postgres, the engine picker, the governance
  fragments, the schedule load) is drawn in HTML with the product's labels.
- The page background is `app/components/OrderingField.vue`, the sign-in page's field of values
  that put themselves in order (`frontend/components/ui/OrderingField.vue`), ported: one fixed
  canvas, fainter, 15 frames a second, paused while the tab is hidden, a still frame under
  reduced motion.
- `public/og.png` — the Open Graph image, drawn from the brand by
  [`scripts/og/og.html`](scripts/og/og.html) (open it in a browser at 1200×630 and take a screenshot).
- Favicons are the brand's (`frontend/public`, from `brand/`); the mark is inlined from
  `brand/mark-*.svg` in [`app/components/BrandMark.vue`](app/components/BrandMark.vue).
