// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  // SSR attivo (default). Vue Flow è client-only: viene isolato in <ClientOnly>
  // dentro components/FlowEditor.vue, così non viene mai renderizzato sul server.
  devtools: { enabled: true },

  runtimeConfig: {
    public: {
      // URL del backend FastAPI (il browser lo chiama direttamente)
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
      // bucket di default sullo storage
      bucket: process.env.NUXT_PUBLIC_BUCKET || 'data-prep',
      // Grafana visto dal browser (iframe della tab Monitoring, solo admin)
      grafanaUrl: process.env.NUXT_PUBLIC_GRAFANA_URL || 'http://localhost:3001',
    },
  },

  // nomi componenti senza prefisso di cartella (SourceNode, DataGrid, ...)
  components: [{ path: '~/components', pathPrefix: false }],

  css: [
    '@vue-flow/core/dist/style.css',
    '@vue-flow/core/dist/theme-default.css',
    '@vue-flow/controls/dist/style.css',
    '@vue-flow/node-resizer/dist/style.css',
    '~/assets/main.css',
  ],

  app: {
    head: {
      title: 'Tabularia',
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
    },
  },
})
