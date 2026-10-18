import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import { site } from './site.config.mjs'
import GithubSlugger from 'github-slugger'
import { githubHeadingIds, rewriteMarkdownLinks } from './lib/doc-links.mjs'
import { isPublished } from './lib/repo-files.mjs'

const repoRoot = resolve(__dirname, '..')

export default defineNuxtConfig({
  compatibilityDate: '2026-10-01',
  devtools: { enabled: false },
  telemetry: false,
  ssr: true,

  modules: ['@nuxt/content'],

  app: {
    baseURL: site.baseURL,
    buildAssetsDir: '_nuxt/',
    head: {
      htmlAttrs: { lang: 'en' },
      charset: 'utf-8',
      viewport: 'width=device-width, initial-scale=1',
      meta: [
        { name: 'theme-color', content: '#0B0E14', media: '(prefers-color-scheme: dark)' },
        { name: 'theme-color', content: '#F3F5F9', media: '(prefers-color-scheme: light)' },
        { name: 'color-scheme', content: 'dark light' },
      ],
      link: [
        { rel: 'icon', type: 'image/svg+xml', href: `${site.baseURL}favicon.svg` },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: `${site.baseURL}favicon-32.png` },
        { rel: 'apple-touch-icon', href: `${site.baseURL}apple-touch-icon.png` },
      ],
      script: [
        // theme before first paint: the visitor's choice, else the system's, else dark
        {
          innerHTML: "try{var t=localStorage.getItem('tabularia-site-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}",
          tagPosition: 'head',
        },
      ],
    },
  },

  css: ['~/assets/css/tokens.css', '~/assets/css/base.css', '~/assets/css/prose.css', '~/assets/css/landing.css'],

  runtimeConfig: {
    public: {
      siteUrl: site.url,
      repo: site.repo,
      branch: site.branch,
      dockerHub: site.dockerHub,
    },
  },

  content: {
    experimental: { sqliteConnector: 'native' },
    watch: { enabled: true },
    renderer: {
      anchorLinks: { h2: true, h3: true, h4: true },
    },
    build: {
      markdown: {
        toc: { depth: 3, searchDepth: 2 },
        highlight: {
          theme: { default: 'github-dark-dimmed', light: 'github-light' },
          langs: ['bash', 'shellscript', 'dockerfile', 'dotenv', 'json', 'python', 'sql', 'yaml', 'ini', 'toml', 'js', 'ts', 'http', 'xml', 'jinja', 'diff'],
        },
      },
    },
  },

  hooks: {
    // Fix the links of the repository's Markdown before it is parsed (see lib/doc-links.mjs).
    'content:file:beforeParse' (ctx: any) {
      const { file, collection } = ctx
      const id = String(file.id || '')
      if (!id.endsWith('.md')) return
      let repoPath: string | null = null
      if (collection.name === 'readme') {
        repoPath = 'README.md'
      } else if (collection.name === 'docs') {
        // id is "<collection>/<prefix>/<file>": "docs/docs/deploy/kubernetes.md"
        const rel = id.replace(/^docs\/docs\//, '')
        // pages written for the site (website/content) are written as if they sat in docs/,
        // so their links resolve the same way
        repoPath = 'docs/' + rel
      }
      if (!repoPath) return
      file.body = rewriteMarkdownLinks(String(file.body), repoPath, { repoRoot, site })
    },
    // GitHub's heading ids, so the docs' own anchors keep working on the site
    'content:file:afterParse' (ctx: any) {
      githubHeadingIds(ctx.content, GithubSlugger)
      // a folder's README is the folder's page: /docs/examples/keycloak
      if (typeof ctx.content.path === 'string') ctx.content.path = ctx.content.path.replace(/\/readme$/, '')
      // the file the page comes from, for "Edit this page": only when it is (or will be) on GitHub
      const id = String(ctx.file?.id || '')
      const rel = id.replace(/^docs\/docs\//, '')
      const source = ctx.collection?.name === 'readme'
        ? 'README.md'
        : existsSync(resolve(repoRoot, 'docs', rel)) ? `docs/${rel}` : `website/content/${rel}`
      ctx.content.meta = { ...(ctx.content.meta || {}), source: isPublished(repoRoot, source) ? source : null }
    },
  },

  nitro: {
    preset: 'static',
    publicAssets: [
      { dir: resolve(repoRoot, 'docs/architecture'), baseURL: 'architecture', maxAge: 0 },
    ],
    prerender: {
      crawlLinks: true,
      failOnError: true,
      routes: ['/', '/docs', '/search.json', '/sitemap.xml', '/robots.txt', '/404.html'],
    },
  },

  experimental: {
    // fetch a page's payload when the visitor shows interest in its link, not for every visible link
    defaults: { nuxtLink: { prefetchOn: { visibility: false, interaction: true } } },
  },

  vite: {
    build: { cssCodeSplit: true },
    // the brand files are read from ../brand
    server: { fs: { allow: [repoRoot] } },
  },
})
