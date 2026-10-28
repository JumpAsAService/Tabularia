// https://nuxt.com/docs/api/configuration/nuxt-config
// `process` is really there: this configuration runs in Node. We declare it instead of
// adding @types/node just for three lines — it is a type declaration, it changes
// nothing at runtime.
declare const process: { env: Record<string, string | undefined> }

export default defineNuxtConfig({
  // SSR on (default). Vue Flow is client-only: it is isolated in <ClientOnly> inside
  // components/FlowEditor.vue, so it is never rendered on the server. DevTools OFF: their
  // RPC channel is not authenticated and the development container has the sources
  // mounted writable (audit 2026-09-19, A4)
  devtools: { enabled: false },

  runtimeConfig: {
    public: {
      // URL of the FastAPI backend (the browser calls it directly)
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
      // default bucket on the storage
      bucket: process.env.NUXT_PUBLIC_BUCKET || 'data-prep',
      // Grafana as the browser sees it (the Monitoring tab's iframe, admin only)
      grafanaUrl: process.env.NUXT_PUBLIC_GRAFANA_URL || 'http://localhost:3001',
      // the notebooks' own address (the gateway's NOTEBOOKS__ORIGIN): the frames of their
      // apps and editors, which the page's Content-Security-Policy must allow
      notebooksOrigin: process.env.NUXT_PUBLIC_NOTEBOOKS_ORIGIN || '',
    },
  },

  // component names without folder prefix (SourceNode, DataGrid, ...)
  components: [{ path: '~/components', pathPrefix: false }],

  vite: {
    // the code editor of the Script nodes: prepared with the other dependencies, so the
    // development server does not reload the page the first time an editor opens
    optimizeDeps: {
      include: [
        'monaco-editor/esm/vs/editor/editor.api',
        'monaco-editor/esm/vs/editor/editor.all.js',
        'monaco-editor/esm/vs/basic-languages/python/python.contribution',
        'monaco-editor/esm/vs/basic-languages/r/r.contribution',
        'monaco-editor/esm/vs/basic-languages/sql/sql.contribution',
        'monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution',
        'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution',
        'monaco-editor/esm/vs/basic-languages/shell/shell.contribution',
      ],
    },
    define: {
      // vue-i18n compiles the messages JIT (an interpreted tree) instead of generating code
      // with `new Function`: server/plugins/csp.ts's Content-Security-Policy does not allow
      // eval, and without this the translations would not compile
      __INTLIFY_JIT_COMPILATION__: true,
    },
  },

  css: [
    '@vue-flow/core/dist/style.css',
    '@vue-flow/core/dist/theme-default.css',
    '@vue-flow/controls/dist/style.css',
    '@vue-flow/minimap/dist/style.css',
    '@vue-flow/node-resizer/dist/style.css',
    '~/assets/main.css',
  ],

  app: {
    head: {
      title: 'Tabularia',
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
      // anti-flash: applies the saved theme BEFORE the first paint (dark by default, the base
      // style already; only "light" must be anticipated so as not to flash)
      script: [
        {
          innerHTML:
            "try{var t=localStorage.getItem('tabularia-theme');if(t&&['light','dracula','monokai','imperium'].indexOf(t)>=0)document.documentElement.setAttribute('data-theme',t)}catch(e){}",
          tagPosition: 'head',
        },
      ],
      link: [
        // favicon.svg: the reduced mark (brand/BRAND.md), colours following the
        // BROWSER's colour scheme; browsers that support it prefer it to the PNGs.
        // The PNG / .ico fallbacks are an Ink tile with the reduced mark (16, 32;
        // the .ico's 48 carries the full mark), so they read on light and dark tab bars.
        { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32.png' },
        { rel: 'icon', type: 'image/png', sizes: '16x16', href: '/favicon-16.png' },
        { rel: 'icon', type: 'image/x-icon', href: '/favicon.ico' },
        { rel: 'apple-touch-icon', sizes: '180x180', href: '/apple-touch-icon.png' },
      ],
    },
  },
})
