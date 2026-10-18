<script setup lang="ts">
const config = useRuntimeConfig()
</script>

<template>
  <header class="docs-bar">
    <NuxtLink to="/" class="docs-bar__brand" aria-label="Tabularia home">
      <BrandMark :size="28" />
      <span class="docs-bar__name">Tabularia</span>
    </NuxtLink>
    <NuxtLink to="/docs" class="docs-bar__section">Docs</NuxtLink>
    <DocsSearch />
    <nav class="docs-bar__links" aria-label="Site">
      <NuxtLink to="/">Product</NuxtLink>
      <a :href="config.public.repo">GitHub</a>
      <ThemeToggle />
    </nav>
  </header>
</template>

<style scoped>
.docs-bar {
  position: sticky; top: 0; z-index: 50; display: flex; align-items: center; gap: 16px;
  height: 64px; padding: 0 16px; background: var(--tile);
}
.docs-bar__brand { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; color: var(--text); }
.docs-bar__name { font-weight: 600; font-size: 18px; letter-spacing: -0.02em; }
.docs-bar__section {
  font-size: 15px; color: var(--soft); text-decoration: none; padding: 4px 10px; border-radius: 6px; background: var(--tile-2);
}
.docs-bar__section:hover { color: var(--text); }
.docs-bar__links { display: flex; align-items: center; gap: 4px; margin-left: auto; }
.docs-bar__links a { color: var(--soft); text-decoration: none; font-size: 15px; padding: 10px 12px; border-radius: 6px; }
.docs-bar__links a:hover { color: var(--text); background: var(--tile-2); }
@media (max-width: 760px) {
  .docs-bar { gap: 8px; padding: 0 8px; }
  .docs-bar__name, .docs-bar__section, .docs-bar__links a { display: none; }
}
</style>
