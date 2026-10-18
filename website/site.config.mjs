// The one place that says where the site lives and where the repository is.
//
// SITE_URL is the only value to change when the site moves (a custom domain later):
//   SITE_URL=https://jumpasaservice.github.io/Tabularia/   → served under /Tabularia/
//   SITE_URL=https://tabularia.example.org/                → served at the root
// The base path the pages are built for is derived from it.

const siteUrl = (process.env.SITE_URL || 'https://jumpasaservice.github.io/Tabularia/').replace(/\/?$/, '/')

export const site = {
  url: siteUrl,
  baseURL: new URL(siteUrl).pathname,
  repo: 'https://github.com/JumpAsAService/Tabularia',
  branch: process.env.SITE_REPO_BRANCH || 'main',
  dockerHub: 'https://hub.docker.com/u/jumpasaservice',
}
