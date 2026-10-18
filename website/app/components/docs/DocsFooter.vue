<script setup lang="ts">
const config = useRuntimeConfig()
</script>

<template>
  <footer class="docs-footer">
    <p>Tabularia is open source under the AGPL-3.0. © 2025–2026 Leonardo Trivelli.</p>
    <p>
      <a :href="config.public.repo">Source on GitHub</a> ·
      <a :href="`${config.public.repo}/issues`">Issues</a> ·
      <a :href="config.public.dockerHub">Images on Docker Hub</a>
    </p>
  </footer>
</template>

<style scoped>
.docs-footer {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 24px;
  margin-top: 96px; padding: 24px 16px 32px; background: var(--tile); color: var(--soft); font-size: 14px;
}
.docs-footer a { color: var(--soft); }
.docs-footer a:hover { color: var(--text); }
</style>
