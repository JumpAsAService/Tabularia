// The one place that says where the site lives and where the repository is.
//
// SITE_URL is the only value to change when the site moves:
//   SITE_URL=https://tabularia.org/                        → served at the root (the default)
//   SITE_URL=https://jumpasaservice.github.io/Tabularia/   → served under /Tabularia/
// The base path the pages are built for is derived from it.

const siteUrl = (process.env.SITE_URL || 'https://tabularia.org/').replace(/\/?$/, '/')

export const site = {
  url: siteUrl,
  baseURL: new URL(siteUrl).pathname,
  repo: 'https://github.com/JumpAsAService/Tabularia',
  branch: process.env.SITE_REPO_BRANCH || 'main',
  dockerHub: 'https://hub.docker.com/u/jumpasaservice',
  // Cloudflare Web Analytics: the site's token (public, it is in every page), from the
  // workflow's SITE_ANALYTICS_TOKEN (a repository variable). Empty = no analytics at all.
  // No cookies and no personal data: visits, pages, countries.
  analyticsToken: (process.env.SITE_ANALYTICS_TOKEN || '').trim(),
}
