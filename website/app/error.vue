<script setup lang="ts">
const props = defineProps<{ error: { statusCode?: number, statusMessage?: string } }>()
const route = useRoute()
const inDocs = computed(() => route.path.startsWith('/docs'))
useSeoMeta({ title: props.error?.statusCode === 404 ? 'Page not found' : 'Error', robots: 'noindex' })
</script>

<template>
  <main id="main" class="err">
    <BrandMark :size="64" />
    <h1>{{ error?.statusCode === 404 ? 'This page is not in the table.' : 'Something went wrong.' }}</h1>
    <p v-if="error?.statusCode === 404 && inDocs">There is no documentation page at this address. The documentation index lists every page, and the search box finds a word in all of them.</p>
    <p v-else-if="error?.statusCode === 404">There is no page at this address. Try the home page or the documentation.</p>
    <p v-else>The page could not be shown. Reloading it may help.</p>
    <p class="err__actions">
      <NuxtLink to="/docs" class="btn btn--accent">Documentation</NuxtLink>
      <NuxtLink to="/" class="btn">Home</NuxtLink>
    </p>
  </main>
</template>

<style scoped>
.err { min-height: 100svh; display: grid; align-content: center; justify-items: start; gap: 20px; max-width: 760px; margin: 0 auto; padding: 32px 16px; }
.err h1 { font-size: var(--fs-h2); line-height: 1.02; letter-spacing: -0.035em; font-weight: 600; }
.err p { color: var(--soft); font-size: var(--fs-body); }
.err__actions { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
